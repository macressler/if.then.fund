import enum
import urllib.parse

from django.db import models, transaction
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required
from django.conf import settings
from enumfields import EnumIntegerField as EnumField

from itfsite.utils import JSONField

# Bring the symbols into this module.
from itfsite.betteruser import UserBase, UserManagerBase, DirectLoginBackend

class NotificationsFrequency(enum.Enum):
	NoNotifications = 0
	DailyNotifications = 1
	WeeklyNotifications = 2

# Override the User model with one that adds additional user profile fields.
class UserManager(UserManagerBase):
	def _get_user_class(self):
		return User
class User(UserBase):
	objects = UserManager() # override with derived class
	notifs_freq  = EnumField(NotificationsFrequency, default=NotificationsFrequency.DailyNotifications, help_text="Now often the user wants to get non-obligatory notifications.")

	class Meta:
		permissions = (
			("see_user_emails", "Can see the email addresses of our users"),
		)

	def twostream_data(self):
		from itfsite.models import Notification
		notifs = Notification.objects.filter(user=self).order_by('-created')[0:30]
		return {
			"notifications": Notification.render(notifs),
		}

	def get_contributorinfo(self):
		# Get the User's most recent ContributorInfo object which
		# will have their name, address, etc.
		from contrib.models import Pledge
		p = Pledge.objects.filter(user=self).order_by('-created').first()
		return p and p.profile

	def active_timezone(self):
		# Activate the user's timezone.
		from contrib.models import Pledge
		from django.utils import timezone
		import pytz

		# Fall back to US Eastern, where our company is located and
		# legislative activity in Congress occurs.
		tz = "America/New_York"

		# See if we have stored a timezone in a recent, geocoded profile.
		pledge = Pledge.objects.filter(user=self, profile__is_geocoded=True).order_by('-created').first()
		if pledge and pledge.profile.extra['geocode'].get('tz'):
			tz = pledge.profile.extra['geocode']['tz']

		# Activate.
		timezone.activate(pytz.timezone(tz))

class AnonymousUser(models.Model):
	"""A class to which to tie multiple actions by a single anonymous user."""

	email = models.EmailField(max_length=254, blank=True, null=True, db_index=True)

	sentConfirmationEmail = models.BooleanField(default=False, help_text="Have we sent this user an email to confirm their address and activate their account/actions?")
	confirmed_user = models.ForeignKey(User, blank=True, null=True, on_delete=models.CASCADE, help_text="The user that this record became confirmed as.")

	created = models.DateTimeField(auto_now_add=True, db_index=True)
	updated = models.DateTimeField(auto_now=True, db_index=True)

	extra = JSONField(blank=True, help_text="Additional information stored with this object.")

	def __str__(self):
		return self.email + ((" → " + str(self.confirmed_user)) if self.confirmed_user else "")

	def send_email_confirmation(self):
		from datetime import timedelta
		from django.utils import timezone

		# Sanity check.
		if self.confirmed_user: raise ValueError("Can't send an email confirmation for an AnonymousUser that has already been confirmed.")

		# Get or create an EmailConfirmation object.
		from email_confirm_la.models import EmailConfirmation
		if not self.sentConfirmationEmail:
			# Make an EmailConfirmation object.
			ec = EmailConfirmation.create(self)
		else:
			# Get an existing EmailConfirmation object.
			ec = EmailConfirmation.get_for(self)

			# Don't send another email confirmation if we just sent one for this user
			# (e.g. if the user took a second action a few minutes later).
			if timezone.now() - ec.sent_at <  timedelta(seconds=60*20):
				return

		# For when we re-send confirmation emails later, ensure we choose
		# an action that isn't already confirmed and wasn't created so long
		# ago that the user will have forgotten what this is about. For
		# Pledges, also don't try to confirm one that is already executed.
		from itfsite.models import CampaignStatus
		from contrib.models import Pledge, PledgeStatus
		def filter_objs(qs):
			return qs.filter(
				user=None,
				anon_user=self,
				created__gt=timezone.now()-timedelta(days=7),
				via_campaign__status=CampaignStatus.Open)\
				.order_by('-created')
		profile = None
		pledge = filter_objs(Pledge.objects).first()
		if pledge:
			template = "contrib/mail/confirm_email"
			profile = pledge.profile
			brand_id = pledge.via_campaign.brand
		else:
			raise ValueError("AnonymousUser is not associated with a Pledge on an open campaign.")

		# Use a custom mailer function so we can send through our
		# HTML emailer app.
		def mailer(context):
			from itfsite.middleware import get_branding
			context.update(get_branding(brand_id))

			context.update({
				"profile": profile, # used in salutation in email_template
				"pledge": pledge,
				"first_try": not self.sentConfirmationEmail,
			})
			
			from htmlemailer import send_mail
			send_mail(
				template,
				context["MAIL_FROM_EMAIL"],
				[context['email']],
				context)

		# Send.
		ec.send(mailer=mailer)

		# Update record.
		self.sentConfirmationEmail = True
		self.save()

	def should_retry_email_confirmation(self):
		from datetime import timedelta
		from django.utils import timezone
		from email_confirm_la.models import EmailConfirmation
		try:
			ec = EmailConfirmation.get_for(self)
		except EmailConfirmation.DoesNotExist as e:
			# The record expired. No need to send again.
			return False
		if ec.send_count >= 3:
			# Already sent three emails, stop.
			return False
		if ec.sent_at < timezone.now() - timedelta(days=1):
			# More than a day has passed since the last email, so send again.
			return True
		return False

	# A user confirms an email address on an anonymous pledge.
	def email_confirmation_confirmed(self, confirmation, request):
		self.email_confirmed(confirmation.email, request)

	@transaction.atomic
	def email_confirmed(self, email, request):
		# Get or create a user account for this person.
		user = User.get_or_create(email)

		# Update this record.
		self.confirmed_user = user
		self.save()

		# Confirm all associated pledges.
		from contrib.models import Pledge
		for pledge in Pledge.objects.filter(anon_user=self):
			pledge.set_confirmed_user(user, request)

		return user

	def email_confirmation_response_view(self, request):
		# The user may be new, so take them to a welcome page.
		from itfsite.middleware import get_branding
		from itfsite.accounts import first_time_confirmed_user

		# Redirect to the most recent pledge of this user on
		# the same brand site as this request is on.
		from contrib.models import Pledge
		pledge = Pledge.objects.filter(
			user=self.confirmed_user,
			via_campaign__brand=get_branding(request)['BRAND_INDEX'],
			).order_by('-created').first()

		return first_time_confirmed_user(request, self.confirmed_user,
			pledge.get_absolute_url() if pledge else "/home")

def first_time_confirmed_user(request, user, next, just_get_url=False):
	# The user has just confirmed their email address. Log them in.
	# If they don't have a password set on their account, welcome them
	# and ask for a password. Otherwise, send them on their way to the
	# next page.

	# Log in.
	user = authenticate(user_object=user)
	if user is None: raise ValueError("Could not authenticate.")
	if not user.is_active: raise ValueError("Account is disabled.")
	login(request, user)

	if not user.has_usable_password():
		url = "/accounts/welcome?" + urllib.parse.urlencode({ "next": next })
		if just_get_url: return url
		return HttpResponseRedirect(url)
	else:
		if just_get_url: return None # no need to go to welcome page
		return HttpResponseRedirect(next)

@login_required
def welcome(request):
	# A welcome page for after an email confirmation to get the user to set a password.
	error = None
	if request.method == "POST":
		p1 = request.POST.get('p1', '')
		p2 = request.POST.get('p2', '')
		if len(p1) < 4 or p1 != p2:
			error = "Validation failed."
		else:
			u = request.user
			try:
				u.set_password(p1)
				u.save()

				# because of SessionAuthenticationMiddleware, the user gets logged
				# out immediately --- log them back in
				u = authenticate(user_object=u)
				login(request, u)

				return HttpResponseRedirect(request.GET.get('next', '/'))
			except:
				error = "Something went wrong, sorry."

	return render(request, "itfsite/welcome.html", {
		"error": error
	})
