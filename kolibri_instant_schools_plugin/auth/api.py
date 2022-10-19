import logging
import os

import requests
from django.db import transaction
from django.utils.translation import ugettext as _
from kolibri.core.auth.api import FacilityUserViewSet
from kolibri.core.auth.api import SignUpViewSet
from kolibri.core.auth.models import FacilityUser
from kolibri.core.auth.serializers import FacilityUserSerializer
from kolibri.core.tasks.decorators import register_task
from rest_framework import serializers
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from ..models import PasswordResetToken
from ..models import PhoneHashToUsernameMapping
from ..smpp.utils import send_password_reset_link
from ..smpp.utils import SMSConnectionError
from .mapping import create_new_username
from .mapping import get_usernames
from .mapping import hash_phone
from .mapping import normalize_phone_number


class PhoneNumberSignupSerializer(FacilityUserSerializer):
    def validate_username(self, value):
        if FacilityUser.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError(
                _(
                    "An account already exists for this phone number. To add a new profile "
                    + "under this account, you must first login. If you have forgotten your "
                    + "password, you can reset it using the link on the login page."
                )
            )
        return value


@register_task()
def send_user_data_to_opco(raw_user_data, **kwargs):
    url = os.environ.get("POST_USER_URL", None)
    if url:
        requests.post(url, data=raw_user_data).raise_for_status()
    else:
        logging.warn(
            "No URL set to post data. If you are expecting to send data to the OpCos,\
                      you should set POST_USER_URL and restart the server. After a week \
                      failures the data will be anonymized and it will be lost permanently"
        )


class PhoneNumberSignUpViewSet(SignUpViewSet):

    serializer_class = PhoneNumberSignupSerializer

    def extract_request_data(self, request):
        data = super(PhoneNumberSignUpViewSet, self).extract_request_data(request)

        # Copy the data so we can create our own job-specific version of it
        job_data = data.copy()
        job_data["failures_count"] = 0
        del job_data["password"]

        # data['username'] is what the user put into the Phone Number field on sign up
        # we're about to hash it, so store it in job_data before hand
        job_data["phone"] = data["username"]

        # if there are already users for this number, use one, to trigger a validation error, else create a new one
        usernames = get_usernames(hash_phone(data["username"]))
        if usernames:
            data["username"] = hash_phone(usernames[0])
        else:
            data["username"] = create_new_username(data["username"])

        # job_data should now have no password, a 0 failures counter and the raw phone number in addition
        # to the normal fields included there
        send_user_data_to_opco.enqueue(job_data)
        return data


class PasswordResetTokenViewset(viewsets.ViewSet):
    def create(self, request):
        """
        Initiate the password reset process by generating a token for a phone number, and sending a link via SMS.

        Usage:

            POST {"phone": "<phone>"} to /user/api/passwordresettoken/
                If it succeeds, it returns status 201.
                If the phone doesn't have an account, it returns status 400, and body is a translated error message.
                If the SMS fails to send, it returns status 500, and the body is a translated error message.
        """

        # extract the phone number from the request
        phone = normalize_phone_number(request.data.get("phone", ""))

        # ensure we have an account for this phone number
        if not get_usernames(phone):
            return Response(
                _("No account found for this phone number."),
                status=status.HTTP_400_BAD_REQUEST,
            )

        # generate a new token for the phone number
        token = PasswordResetToken.generate_new_token(phone=phone)

        # determine base URL from the scheme/host in this request, so we send people back to a server they can access
        baseurl = "{scheme}://{host}".format(
            scheme=request.scheme, host=request.get_host()
        )

        # send the password reset URL to the phone number via SMS
        try:
            send_password_reset_link(phone, token.token, baseurl)
        except SMSConnectionError:
            return Response(
                _("Error sending SMS message; please try again later."),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # return a 201 to indicate having successfully sent the reset URL
        return Response("", status=status.HTTP_201_CREATED)

    def retrieve(self, request, pk=None):
        """
        Check validity of token (from the /user/#/passwordreset/<phone>/<token> page).

        Usage:

            GET `/user/api/passwordresettoken/<token>/?phone=<phone>`
                If token exists and is valid, returns status 200.
                Otherwise, returns status 400.
        """
        phone = normalize_phone_number(request.query_params.get("phone", ""))

        try:
            obj = PasswordResetToken.objects.get(token=pk, phone=phone)
        except PasswordResetToken.DoesNotExist:
            obj = None
        if obj and obj.is_valid():
            return Response("OK", status=status.HTTP_200_OK)
        else:
            return Response("", status=status.HTTP_400_BAD_REQUEST)


class FacilityUserProfileViewset(FacilityUserViewSet):
    def set_password_if_needed(self, instance, serializer):
        with transaction.atomic():
            if serializer.validated_data.get("password", ""):
                # update the password for all accounts associated with this password
                hashed_phone = PhoneHashToUsernameMapping.objects.get(
                    username=instance.username
                ).hash
                set_password_for_hashed_phone(
                    hashed_phone, serializer.validated_data["password"]
                )
                # explicitly update password for this user to avoid sign out
                instance.set_password(serializer.validated_data["password"])
                instance.save()


def set_password_for_hashed_phone(hashed_phone, password):

    # get the full list of usernames associated with this account
    usernames = get_usernames(hashed_phone)

    # update the password for each of the accounts
    for user in FacilityUser.objects.filter(username__in=usernames):
        user.set_password(password)
        user.save()


class PasswordChangeViewset(viewsets.ViewSet):
    def create(self, request):
        """
        Change the password for the full set of FacilityUser profiles associated with a phone number.

        Usage:

            POST {"password": "<password>", "phone": "<phone>", "token": "<token>"} to /user/api/passwordchange/

                If it succeeds, it returns status 200.
                If user is not logged in (or doesn't match phone) and no valid token provided, returns status 401.
        """

        # extract the token, password, and phone number from the request data
        token = request.data["token"]
        password = request.data["password"]
        phone = request.data["phone"]

        # try to find a reset token matching the phone number and token, otherwise error out
        try:
            resettoken = PasswordResetToken.objects.get(phone=phone, token=token)
        except PasswordResetToken.DoesNotExist:
            return Response("", status=status.HTTP_401_UNAUTHORIZED)

        with transaction.atomic():
            # mark the token as having been used
            resettoken.use_token()

            # change the password for all accounts associated with this phone number
            set_password_for_hashed_phone(hash_phone(phone), password)

        # return a 200 to indicate having successfully changed the passwords
        return Response("", status=status.HTTP_200_OK)


class PhoneAccountProfileViewset(viewsets.ViewSet):
    def create(self, request):
        """
        Create a new "profile" (FacilityUser) for a given phone number.

        Usage:

            POST {"phone": "<phone>", "password": "<password>", "full_name": "<full_name>"} to /user/api/phoneaccountprofile/
                If it succeeds, it returns status 201 with the new username in the body.
                If it fails, it returns status 401.
        """

        # extract the data from the request
        phone = normalize_phone_number(request.data["phone"])
        password = request.data["password"]
        full_name = request.data["full_name"]

        # get the list of existing profiles (users) for the phone number
        users = FacilityUser.objects.filter(
            username__in=get_usernames(hash_phone(phone))
        )

        if not users:
            return Response("", status=status.HTTP_401_UNAUTHORIZED)

        # verify that the password provided matches the existing password associated with the phone number
        if not users[0].check_password(password):
            return Response("", status=status.HTTP_401_UNAUTHORIZED)

        # generate a new username for this phone number
        username = create_new_username(phone)

        # create the new FacilityUser for the profile
        user = FacilityUser(
            username=username, full_name=full_name, facility=users[0].facility
        )
        user.set_password(password)
        user.save()

        return Response(username, status=status.HTTP_201_CREATED)

    @action(methods=["POST"], detail=False)
    def profiles(self, request):
        """
        Get a list of profiles associated with a phone number (authenticated by password).

        Usage:

            POST {"phone": "<phone>", "password": "<password>"} to /user/api/phoneaccountprofile/
                If successful, returns status 200 with a list of dicts with `full_name` and `username`.
                If no accounts are found, returns status 404.
                If password fails, returns status 401.
        """
        # extract the phone and password from the query params
        phone = normalize_phone_number(request.data.get("phone", ""))
        password = request.data.get("password", "")

        # get all user profiles associated with the phone number
        users = FacilityUser.objects.filter(
            username__in=get_usernames(hash_phone(phone))
        )

        # return a 404 if there are no users for this phone number
        if not users:
            return Response("", status=status.HTTP_404_NOT_FOUND)

        # verify that the password provided matches the existing password associated with the phone number
        if not users[0].check_password(password):
            return Response("", status=status.HTTP_401_UNAUTHORIZED)

        return Response(
            [
                {"username": user.username, "full_name": user.full_name}
                for user in users
            ],
            status=status.HTTP_200_OK,
        )
