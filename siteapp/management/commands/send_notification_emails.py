from django.core.management.base import BaseCommand, CommandError
from django.db import transaction, models
from django.conf import settings
from django.utils import timezone

import time, uuid

from exclusiveprocess import Lock

from notifications.models import Notification

class Command(BaseCommand):
    help = 'Sends emails for notifications.'

    def add_arguments(self, parser):
        parser.add_argument('forever', nargs='?', type=bool)

    def handle(self, *args, **options):
        # Ensure this process doesn't run multiple times concurrently.
        Lock(die=True).forever()

        if options["forever"]:
            # Loop forever.
            while True:
                self.send_new_emails()
                time.sleep(20)

        else:
            # Run on-off job.
            self.send_new_emails()


    def send_new_emails(self):
        # Find notifications that have not been emailed but should be emailed.
        #  * The user has notifications enabled.
        #  * The notification is more recent than the last notification email sent.
        #  * The notification has target object so that we can generate a link back
        #    to the site.
        # And go in order because once we mark a notification as emailed, we imply
        # that all earlier notifications have been sent to the user too.
        notifs = Notification.objects\
            .filter(
                recipient__notifemails_enabled=0,
                id__gt=models.F('recipient__notifemails_last_notif_id')
            )\
            .exclude(target_object_id=None)\
            .order_by('id')

        for notif in notifs:
            self.send_it_out(notif)

    def send_it_out(self, notif):
        # If the Notification's target does not have an 'organization' attribute
        # and a 'get_absolute_url' attribute, then we can't generate a link back
        # to the site, so skip it.
        target = notif.target
        if not hasattr(target, 'get_absolute_url'): return
        organization = getattr(notif.target, 'organization', None)
        if not organization: return

        # Let the actor render appropriate for the org.
        notif.actor.localize_to_org(organization)

        # If the target supports receiving email replies (like replying to an email
        # about a discussion), then store a secret in the notif.data dictionary so
        # that we can tell that a user has replied to something we sent them (and
        # can't reply to something we didn't notify them about).
        what_reply_does = None
        if hasattr(target, "post_notification_reply"):
            notif.data = notif.data or { }
            notif.data["secret_key"] = uuid.uuid4()
            notif.save(update_fields=['data'])
            what_reply_does = "You can reply to this email to post a comment to the discussion. Do not forward this email to others."

        # Send the email.
        from htmlemailer import send_mail
        from email.utils import format_datetime
        from siteapp.templatetags.notification_helpers import get_notification_link
        url = get_notification_link(target, notif)
        if url is None:
            # Some notifications go stale and can't generate links,
            # and then we can't email notifications.
            return
        send_mail(
            "email/notification",
            settings.DEFAULT_FROM_EMAIL,
            [notif.recipient.email],
            {
                "notification": notif,
                "url": organization.get_url(url),
                "whatreplydoes": what_reply_does,
            },
            headers={
                "From": settings.NOTIFICATION_FROM_EMAIL_PATTERN % (str(notif.actor),),
                "Reply-To": (settings.NOTIFICATION_REPLY_TO_EMAIL_PATTERN % (organization.name, notif.id, notif.data["secret_key"]))
                if what_reply_does else "",
                "Date": format_datetime(notif.timestamp),
            }
        )

        # Mark it as sent.
        notif.recipient.notifemails_last_notif_id = notif.id
        notif.recipient.notifemails_last_at = timezone.now()
        notif.recipient.save(update_fields=['notifemails_last_notif_id', 'notifemails_last_at'])

