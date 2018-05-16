"""
Programmatic integration point for User API Accounts sub-application
"""
import os
import random
import uuid
from django.utils.translation import ugettext as _
from django.db import transaction, IntegrityError
import datetime
import hashlib
from pytz import UTC
from django.core.exceptions import ObjectDoesNotExist
from django.conf import settings
from django.core.validators import validate_email, validate_slug, ValidationError
from social.apps.django_app.default.models import UserSocialAuth

from edxmako.shortcuts import render_to_string
from openedx.core.djangoapps.user_api.preferences.api import update_user_preferences
from openedx.core.djangoapps.user_api.errors import PreferenceValidationError
from student.models import User, UserProfile, Registration
from student import views as student_views
from third_party_auth.models import UserSocialAuthMapping
from util.model_utils import emit_setting_changed_event
import lms.lib.comment_client as cc
from openedx.core.lib.api.view_utils import add_serializer_errors

from ..errors import (
    AccountUpdateError, AccountValidationError, AccountUsernameInvalid, AccountPasswordInvalid,
    AccountEmailInvalid, AccountUserAlreadyExists,
    UserAPIInternalError, UserAPIRequestError, UserNotFound, UserNotAuthorized
)
from ..forms import PasswordResetFormNoActive
from ..helpers import intercept_errors

from . import (
    EMAIL_MIN_LENGTH, EMAIL_MAX_LENGTH, PASSWORD_MIN_LENGTH, PASSWORD_MAX_LENGTH,
    USERNAME_MIN_LENGTH, USERNAME_MAX_LENGTH
)
from .serializers import (
    AccountLegacyProfileSerializer, AccountUserSerializer,
    UserReadOnlySerializer, _visible_fields  # pylint: disable=invalid-name
)
from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers
from lms.lib.comment_client.thread import Thread
from lms.lib.comment_client.user import User as ThreadUser
from lms.djangoapps.courseware.courses import get_courses
from discussion_api.serializers import CommentSerializer, ThreadSerializer


# Public access point for this function.
visible_fields = _visible_fields


@intercept_errors(UserAPIInternalError, ignore_errors=[UserAPIRequestError])
def get_account_settings(request, usernames=None, configuration=None, view=None):
    """Returns account information for a user serialized as JSON.

    Note:
        If `request.user.username` != `username`, this method will return differing amounts of information
        based on who `request.user` is and the privacy settings of the user associated with `username`.

    Args:
        request (Request): The request object with account information about the requesting user.
            Only the user with username `username` or users with "is_staff" privileges can get full
            account information. Other users will get the account fields that the user has elected to share.
        usernames (list): Optional list of usernames for the desired account information. If not
            specified, `request.user.username` is assumed.
        configuration (dict): an optional configuration specifying which fields in the account
            can be shared, and the default visibility settings. If not present, the setting value with
            key ACCOUNT_VISIBILITY_CONFIGURATION is used.
        view (str): An optional string allowing "is_staff" users and users requesting their own
            account information to get just the fields that are shared with everyone. If view is
            "shared", only shared account information will be returned, regardless of `request.user`.

    Returns:
         A list of users account details.

    Raises:
         UserNotFound: no user with username `username` exists (or `request.user.username` if
            `username` is not specified)
         UserAPIInternalError: the operation failed due to an unexpected error.
    """
    requesting_user = request.user
    usernames = usernames or [requesting_user.username]

    requested_users = User.objects.select_related('profile').filter(username__in=usernames)
    if not requested_users:
        raise UserNotFound()

    serialized_users = []
    for user in requested_users:
        has_full_access = requesting_user.is_staff or requesting_user.username == user.username
        if has_full_access and view != 'shared':
            admin_fields = settings.ACCOUNT_VISIBILITY_CONFIGURATION.get('admin_fields')
        else:
            admin_fields = None
        serialized_users.append(UserReadOnlySerializer(
            user,
            configuration=configuration,
            custom_fields=admin_fields,
            context={'request': request}
        ).data)

    return serialized_users


@intercept_errors(UserAPIInternalError, ignore_errors=[UserAPIRequestError])
def update_account_settings(requesting_user, update, username=None, force_email_update=False):
    """Update user account information.

    Note:
        It is up to the caller of this method to enforce the contract that this method is only called
        with the user who made the request.

    Arguments:
        requesting_user (User): The user requesting to modify account information. Only the user with username
            'username' has permissions to modify account information.
        update (dict): The updated account field values.
        username (str): Optional username specifying which account should be updated. If not specified,
            `requesting_user.username` is assumed.
        force_email_update (bool): Optional flag, update the user's email address if this flag
            is set with the ENABLE_MSA_MIGRATION flag in settings/site config

    Raises:
        UserNotFound: no user with username `username` exists (or `requesting_user.username` if
            `username` is not specified)
        UserNotAuthorized: the requesting_user does not have access to change the account
            associated with `username`
        AccountValidationError: the update was not attempted because validation errors were found with
            the supplied update
        AccountUpdateError: the update could not be completed. Note that if multiple fields are updated at the same
            time, some parts of the update may have been successful, even if an AccountUpdateError is returned;
            in particular, the user account (not including e-mail address) may have successfully been updated,
            but then the e-mail change request, which is processed last, may throw an error.
        UserAPIInternalError: the operation failed due to an unexpected error.
    """
    if username is None:
        username = requesting_user.username

    existing_user, existing_user_profile = _get_user_and_profile(username)

    if requesting_user.username != username:
        raise UserNotAuthorized()

    # If user has requested to change email, we must call the multi-step process to handle this.
    # It is not handled by the serializer (which considers email to be read-only).
    changing_email = False
    if "email" in update:
        changing_email = True
        new_email = update["email"]
        del update["email"]

    # If user has requested to change name, store old name because we must update associated metadata
    # after the save process is complete.
    old_name = None
    if "name" in update:
        old_name = existing_user_profile.name

    # Check for fields that are not editable. Marking them read-only causes them to be ignored, but we wish to 400.
    read_only_fields = set(update.keys()).intersection(
        AccountUserSerializer.get_read_only_fields() + AccountLegacyProfileSerializer.get_read_only_fields()
    )

    # Build up all field errors, whether read-only, validation, or email errors.
    field_errors = {}

    if read_only_fields:
        for read_only_field in read_only_fields:
            field_errors[read_only_field] = {
                "developer_message": u"This field is not editable via this API",
                "user_message": _(u"The '{field_name}' field cannot be edited.").format(field_name=read_only_field)
            }
            del update[read_only_field]

    user_serializer = AccountUserSerializer(existing_user, data=update)
    legacy_profile_serializer = AccountLegacyProfileSerializer(existing_user_profile, data=update)

    for serializer in user_serializer, legacy_profile_serializer:
        field_errors = add_serializer_errors(serializer, update, field_errors)

    # If the user asked to change email, validate it.
    if changing_email:
        try:
            student_views.validate_new_email(existing_user, new_email)
        except ValueError as err:
            field_errors["email"] = {
                "developer_message": u"Error thrown from validate_new_email: '{}'".format(err.message),
                "user_message": err.message
            }

    # If we have encountered any validation errors, return them to the user.
    if field_errors:
        raise AccountValidationError(field_errors)

    try:
        # If everything validated, go ahead and save the serializers.

        # We have not found a way using signals to get the language proficiency changes (grouped by user).
        # As a workaround, store old and new values here and emit them after save is complete.
        if "language_proficiencies" in update:
            old_language_proficiencies = legacy_profile_serializer.data["language_proficiencies"]

        for serializer in user_serializer, legacy_profile_serializer:
            serializer.save()

        # if any exception is raised for user preference (i.e. account_privacy), the entire transaction for user account
        # patch is rolled back and the data is not saved
        if 'account_privacy' in update:
            update_user_preferences(
                requesting_user, {'account_privacy': update["account_privacy"]}, existing_user
            )

        if "language_proficiencies" in update:
            new_language_proficiencies = update["language_proficiencies"]
            emit_setting_changed_event(
                user=existing_user,
                db_table=existing_user_profile.language_proficiencies.model._meta.db_table,
                setting_name="language_proficiencies",
                old_value=old_language_proficiencies,
                new_value=new_language_proficiencies,
            )

        # If the name was changed, store information about the change operation. This is outside of the
        # serializer so that we can store who requested the change.
        if old_name:
            meta = existing_user_profile.get_meta()
            if 'old_names' not in meta:
                meta['old_names'] = []
            meta['old_names'].append([
                old_name,
                u"Name change requested through account API by {0}".format(requesting_user.username),
                datetime.datetime.now(UTC).isoformat()
            ])
            existing_user_profile.set_meta(meta)
            existing_user_profile.save()

    except PreferenceValidationError as err:
        raise AccountValidationError(err.preference_errors)
    except Exception as err:
        raise AccountUpdateError(
            u"Error thrown when saving account updates: '{}'".format(err.message)
        )
    meta = existing_user_profile.get_meta()
    msa_migration_enabled = configuration_helpers.get_value("ENABLE_MSA_MIGRATION")
    # And try to send the email change request if necessary.
    if changing_email:
        if force_email_update and msa_migration_enabled:
            # If MSA Migration is enabled and we're coming through
            # the link/account/confirm page ajax call, force update the user's email.
            with transaction.atomic():
                address_context = {
                    'old_email': existing_user.email,
                    'new_email': new_email
                }

                if len(User.objects.filter(email=new_email)) != 0:
                    transaction.set_rollback(True)
                    raise AccountUserAlreadyExists

                subject = render_to_string('emails/email_change_subject.txt', address_context)
                subject = ''.join(subject.splitlines())
                message = render_to_string('emails/confirm_email_change.txt', address_context)
                existing_user_profile.set_meta(meta)
                existing_user_profile.save()
                # Send it to the old email...
                try:
                    existing_user.email_user(
                        subject,
                        message,
                        configuration_helpers.get_value('email_from_address', settings.DEFAULT_FROM_EMAIL)
                    )
                except Exception:  # pylint: disable=broad-except
                    transaction.set_rollback(True)
                    raise AccountUpdateError(
                        u"Error thrown from emailing old email address for user: '{}'".format(err.message),
                        user_message=err.message
                    )

                existing_user.email = new_email
                # Explicitly activate any non-active user, validated through migration already
                existing_user.is_active = True
                existing_user.save()

                # And send it to the new email...
                try:
                    existing_user.email_user(
                        subject,
                        message,
                        configuration_helpers.get_value('email_from_address', settings.DEFAULT_FROM_EMAIL)
                    )
                except Exception:  # pylint: disable=broad-except
                    transaction.set_rollback(True)
                    raise AccountUpdateError(
                        u"Error thrown from emailing new email address for user: '{}'".format(err.message),
                        user_message=err.message
                    )
        else:
            try:
                student_views.do_email_change_request(existing_user, new_email)
            except ValueError as err:
                raise AccountUpdateError(
                    u"Error thrown from do_email_change_request: '{}'".format(err.message),
                    user_message=err.message
                )
    if force_email_update and msa_migration_enabled:
        try:
            # Flag to show user has completed and confirmed Microsoft Account Migration
            meta[settings.MSA_ACCOUNT_MIGRATION_STATUS_KEY] = settings.MSA_MIGRATION_STATUS_COMPLETED
            existing_user_profile.set_meta(meta)
            existing_user_profile.save()
        except Exception as err:  # pylint: disable=broad-except
            transaction.set_rollback(True)
            raise AccountUpdateError(
                u"Error saving user confirmation: '{}'".format(err.message),
                user_message=err.message
            )


def _get_user_and_profile(username):
    """
    Helper method to return the legacy user and profile objects based on username.
    """
    try:
        existing_user = User.objects.get(username=username)
        existing_user_profile = UserProfile.objects.get(user=existing_user)
    except ObjectDoesNotExist:
        raise UserNotFound()

    return existing_user, existing_user_profile


@intercept_errors(UserAPIInternalError, ignore_errors=[UserAPIRequestError])
@transaction.atomic
def create_account(username, password, email):
    """Create a new user account.

    This will implicitly create an empty profile for the user.

    WARNING: This function does NOT yet implement all the features
    in `student/views.py`.  Until it does, please use this method
    ONLY for tests of the account API, not in production code.
    In particular, these are currently missing:

    * 3rd party auth
    * External auth (shibboleth)
    * Complex password policies (ENFORCE_PASSWORD_POLICY)

    In addition, we assume that some functionality is handled
    at higher layers:

    * Analytics events
    * Activation email
    * Terms of service / honor code checking
    * Recording demographic info (use profile API)
    * Auto-enrollment in courses (if invited via instructor dash)

    Args:
        username (unicode): The username for the new account.
        password (unicode): The user's password.
        email (unicode): The email address associated with the account.

    Returns:
        unicode: an activation key for the account.

    Raises:
        AccountUserAlreadyExists
        AccountUsernameInvalid
        AccountEmailInvalid
        AccountPasswordInvalid
        UserAPIInternalError: the operation failed due to an unexpected error.
    """
    # Validate the username, password, and email
    # This will raise an exception if any of these are not in a valid format.
    _validate_username(username)
    _validate_password(password, username)
    _validate_email(email)

    # Create the user account, setting them to "inactive" until they activate their account.
    user = User(username=username, email=email, is_active=False)
    user.set_password(password)

    try:
        user.save()
    except IntegrityError:
        raise AccountUserAlreadyExists

    # Create a registration to track the activation process
    # This implicitly saves the registration.
    registration = Registration()
    registration.register(user)

    # Create an empty user profile with default values
    UserProfile(user=user).save()

    # Return the activation key, which the caller should send to the user
    return registration.activation_key


def check_account_exists(username=None, email=None):
    """Check whether an account with a particular username or email already exists.

    Keyword Arguments:
        username (unicode)
        email (unicode)

    Returns:
        list of conflicting fields

    Example Usage:
        >>> account_api.check_account_exists(username="bob")
        []
        >>> account_api.check_account_exists(username="ted", email="ted@example.com")
        ["email", "username"]

    """
    conflicts = []

    if email is not None and User.objects.filter(email=email).exists():
        conflicts.append("email")

    if username is not None and User.objects.filter(username=username).exists():
        conflicts.append("username")

    return conflicts


@intercept_errors(UserAPIInternalError, ignore_errors=[UserAPIRequestError])
def activate_account(activation_key):
    """Activate a user's account.

    Args:
        activation_key (unicode): The activation key the user received via email.

    Returns:
        None

    Raises:
        UserNotAuthorized
        UserAPIInternalError: the operation failed due to an unexpected error.
    """
    try:
        registration = Registration.objects.get(activation_key=activation_key)
    except Registration.DoesNotExist:
        raise UserNotAuthorized
    else:
        # This implicitly saves the registration
        registration.activate()


@intercept_errors(UserAPIInternalError, ignore_errors=[UserAPIRequestError])
def request_password_change(email, is_secure):
    """Email a single-use link for performing a password reset.

    Users must confirm the password change before we update their information.

    Args:
        email (str): An email address
        orig_host (str): An originating host, extracted from a request with get_host
        is_secure (bool): Whether the request was made with HTTPS

    Returns:
        None

    Raises:
        UserNotFound
        AccountRequestError
        UserAPIInternalError: the operation failed due to an unexpected error.
    """
    # Binding data to a form requires that the data be passed as a dictionary
    # to the Form class constructor.
    form = PasswordResetFormNoActive({'email': email})

    # Validate that a user exists with the given email address.
    if form.is_valid():
        # Generate a single-use link for performing a password reset
        # and email it to the user.
        form.save(
            from_email=configuration_helpers.get_value('email_from_address', settings.DEFAULT_FROM_EMAIL),
            use_https=is_secure
        )
    else:
        # No user with the provided email address exists.
        raise UserNotFound


def _validate_username(username):
    """Validate the username.

    Arguments:
        username (unicode): The proposed username.

    Returns:
        None

    Raises:
        AccountUsernameInvalid

    """
    if not isinstance(username, basestring):
        raise AccountUsernameInvalid(u"Username must be a string")

    if len(username) < USERNAME_MIN_LENGTH:
        raise AccountUsernameInvalid(
            u"Username '{username}' must be at least {min} characters long".format(
                username=username,
                min=USERNAME_MIN_LENGTH
            )
        )
    if len(username) > USERNAME_MAX_LENGTH:
        raise AccountUsernameInvalid(
            u"Username '{username}' must be at most {max} characters long".format(
                username=username,
                max=USERNAME_MAX_LENGTH
            )
        )
    try:
        validate_slug(username)
    except ValidationError:
        raise AccountUsernameInvalid(
            u"Username '{username}' must contain only A-Z, a-z, 0-9, -, or _ characters"
        )


def _validate_password(password, username):
    """Validate the format of the user's password.

    Passwords cannot be the same as the username of the account,
    so we take `username` as an argument.

    Arguments:
        password (unicode): The proposed password.
        username (unicode): The username associated with the user's account.

    Returns:
        None

    Raises:
        AccountPasswordInvalid

    """
    if not isinstance(password, basestring):
        raise AccountPasswordInvalid(u"Password must be a string")

    if len(password) < PASSWORD_MIN_LENGTH:
        raise AccountPasswordInvalid(
            u"Password must be at least {min} characters long".format(
                min=PASSWORD_MIN_LENGTH
            )
        )

    if len(password) > PASSWORD_MAX_LENGTH:
        raise AccountPasswordInvalid(
            u"Password must be at most {max} characters long".format(
                max=PASSWORD_MAX_LENGTH
            )
        )

    if password == username:
        raise AccountPasswordInvalid(u"Password cannot be the same as the username")


def _validate_email(email):
    """Validate the format of the email address.

    Arguments:
        email (unicode): The proposed email.

    Returns:
        None

    Raises:
        AccountEmailInvalid

    """
    if not isinstance(email, basestring):
        raise AccountEmailInvalid(u"Email must be a string")

    if len(email) < EMAIL_MIN_LENGTH:
        raise AccountEmailInvalid(
            u"Email '{email}' must be at least {min} characters long".format(
                email=email,
                min=EMAIL_MIN_LENGTH
            )
        )

    if len(email) > EMAIL_MAX_LENGTH:
        raise AccountEmailInvalid(
            u"Email '{email}' must be at most {max} characters long".format(
                email=email,
                max=EMAIL_MAX_LENGTH
            )
        )

    try:
        validate_email(email)
    except ValidationError:
        raise AccountEmailInvalid(
            u"Email '{email}' format is not valid".format(email=email)
        )


@intercept_errors(UserAPIInternalError, ignore_errors=[UserAPIRequestError])
def delete_user_account(username):
    """
    Soft delete a user's account.
    Associated records that would allow the user to be identified or continue to login
    are deleted from the database.
    However, the data in the auth_user table is only made anonymous.
    This allows us to keep course progress and other associated data for the user for
    analytics and reporting without keeping any PII data.

    Keyword Arguments:
        username - username of user to delete

    Raises:
        UserNotFound

    Example Usage:
        >>> delete_user_account('karen2112')

    """

    existing_user, existing_user_profile = _get_user_and_profile(username)

    # If we get here the user must have a profile, delete this record
    existing_user_profile.delete()

    # Delete user's social auth records if they exist
    social_auth_records = UserSocialAuth.objects.filter(user=existing_user)
    for social_auth_record in social_auth_records:
        social_auth_record.delete()

    # Delete user's Microsoft Live account PUID mapping if it exists
    try:
        social_auth_mapping = UserSocialAuthMapping.objects.get(user=existing_user)
        social_auth_mapping.delete()
    except Exception:
        # This error is most likely a *.DoesNotExist,
        # meaning the user does not have a social auth record
        # We don't need to do anything special here and
        # should NOT raise an exception
        pass

    # Anonymize the user's records
    username_mask = str(random.randint(1, 9999)) + username
    existing_user.username = hashlib.md5(username_mask).hexdigest()
    existing_user.email = existing_user.username + "@deleteduser.com"
    existing_user.first_name = 'first_deleted'
    existing_user.last_name = 'last_deleted'
    existing_user.is_active = False
    existing_user.is_staff = False
    existing_user.save()
    # Anonymize forum discussions
    try:
        anonymize_user_discussions(existing_user, username)
    except Exception:
        pass

    # Successful user soft delete
    return True


def anonymize_user_discussions(user, old_username):
    """
    Anonymize user's comments for a particular user as per GDPR norms.
    This updates the "users" and "contents" collections of "cs_comments_service"
    with the masked username. It also anonymizes the comments and threads of
    this particular user.

    Keyword Arguments:
        username (unicode)

    Example Usage:
        >>> anonymize_user_discussions(122, 'staff', '570810a83dee178ca19a05f2838839')
    """

    # Getting all courses for the user
    courses = get_courses(user)
    # Updating discussion user instance
    updated_user = ThreadUser.from_django_user(user)
    updated_user.save()
    # Updating each discussion entity for each course
    query_params = {
        'paged_results': False,
        'author_username': old_username,
        'retired_username': user.username
    }
    for course in courses:
        query_params['course_id'] = str(course.id)
        discussion_entities = Thread.search(query_params)
        for entity in discussion_entities.collection:
            # 'pinned' key needs to be removed
            # before update as its read-only
            del entity['pinned']
            # Initializing thread for update
            th = Thread()
            th.id = entity['id']
            entity['anonymous'] = True
            entity['anonymous_to_peers'] = True
            entity['author_username'] = user.username
            th.save(entity)
    # Anonymize all the replys to threads by user
    profiled_user = cc.User(id=user.id)
    profiled_user.retire_threads(query_params)
