"""Self-contained cron module: user-owned recurring prompts on a schedule.

The module boundary is `octoforge_core.cron.api`: the `CronStore` protocol,
the `CronWaker` port, the DTO and the schedule math. Everything else (SQL
storage, the asyncio scheduler) is an implementation detail; the store can be
extracted behind an HTTP boundary later without changing call sites.
"""
