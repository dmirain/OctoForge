"""HTTP representation of a cron job."""

from octoforge_core.cron.api import CronJob

from octoforge_server.api.schemas import CronJobResponse


def cron_response(job: CronJob) -> CronJobResponse:
    return CronJobResponse(
        id=job.id,
        user_id=job.user_id,
        channel=job.channel,
        title=job.title,
        schedule=job.schedule,
        timezone=job.timezone,
        prompt=job.prompt,
        enabled=job.enabled,
        next_fire_at=job.next_fire_at,
        last_fire_at=job.last_fire_at,
        created_at=job.created_at,
        one_shot=job.one_shot,
        last_status=None if job.last_status is None else job.last_status.value,
        last_error=job.last_error,
        retry_count=job.retry_count,
    )
