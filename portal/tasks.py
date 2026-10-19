from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import IntegrationOutbox, Payment
from .services.registration_portal import PortalIntegrationError, RegistrationPortalAdapter


@shared_task
def sync_payment_to_portal(outbox_id):
    with transaction.atomic():
        outbox = IntegrationOutbox.objects.select_for_update().select_related("payment").get(pk=outbox_id)
        if outbox.status == IntegrationOutbox.Status.SYNCED:
            return True
        if outbox.status == IntegrationOutbox.Status.PROCESSING and outbox.updated_at > timezone.now() - timedelta(minutes=10):
            return False
        outbox.status = IntegrationOutbox.Status.PROCESSING
        outbox.attempts += 1
        outbox.save(update_fields=["status", "attempts", "updated_at"])
        payment_id = outbox.payment_id
    try:
        payment = Payment.objects.select_related("student", "term__session").get(pk=payment_id)
        RegistrationPortalAdapter().sync_payment(payment)
    except Exception as error:
        with transaction.atomic():
            outbox = IntegrationOutbox.objects.select_for_update().get(pk=outbox_id)
            outbox.last_error = str(error)[:2000]
            outbox.status = IntegrationOutbox.Status.FAILED if outbox.attempts >= 12 else IntegrationOutbox.Status.PENDING
            outbox.next_attempt_at = timezone.now() + timedelta(seconds=min(60 * (2 ** min(outbox.attempts, 10)), 86400))
            outbox.save(update_fields=["last_error", "status", "next_attempt_at", "updated_at"])
            Payment.objects.filter(pk=payment_id).update(sync_status=Payment.SyncStatus.FAILED)
        return False
    with transaction.atomic():
        outbox = IntegrationOutbox.objects.select_for_update().get(pk=outbox_id)
        outbox.status = IntegrationOutbox.Status.SYNCED
        outbox.last_error = ""
        outbox.save(update_fields=["status", "last_error", "updated_at"])
        Payment.objects.filter(pk=payment_id).update(sync_status=Payment.SyncStatus.SYNCED)
    return True


@shared_task
def retry_pending_outbox():
    due_ids = list(IntegrationOutbox.objects.filter(status=IntegrationOutbox.Status.PENDING, next_attempt_at__lte=timezone.now()).values_list("id", flat=True)[:100])
    stale_before = timezone.now() - timedelta(minutes=10)
    stale_ids = list(IntegrationOutbox.objects.filter(status=IntegrationOutbox.Status.PROCESSING, updated_at__lt=stale_before).values_list("id", flat=True)[:100])
    for outbox_id in due_ids + stale_ids:
        sync_payment_to_portal.delay(outbox_id)
    return len(due_ids) + len(stale_ids)