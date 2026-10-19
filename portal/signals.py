from django.db.models.signals import post_save
from django.db import transaction
from django.dispatch import receiver

from .models import IntegrationOutbox


@receiver(post_save, sender=IntegrationOutbox)
def enqueue_new_outbox(sender, instance, created, **kwargs):
    if created:
        from .tasks import sync_payment_to_portal

        def enqueue():
            try:
                sync_payment_to_portal.delay(instance.pk)
            except Exception:
                pass

        transaction.on_commit(enqueue)