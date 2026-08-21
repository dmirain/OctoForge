"""Assemble Telegram poller access, command and media collaborators."""

import asyncio

from octoforge_telegram.client import TelegramClient
from octoforge_telegram.gateway import GatewayRegistry
from octoforge_telegram.message_dispatch import DispatchServices, MessageDispatcher
from octoforge_telegram.poller_access import AccessServices, PollerAccess
from octoforge_telegram.poller_activity import IngestionActivity
from octoforge_telegram.poller_commands import CommandServices, PollerCommands
from octoforge_telegram.poller_images import ImageServices, ImageSubmitter
from octoforge_telegram.poller_material import MaterialDispatcher, MaterialServices
from octoforge_telegram.poller_types import TelegramPollerOptions
from octoforge_telegram.poller_voice import VoiceServices, VoiceSubmitter
from octoforge_telegram.poller_voice_outcome import (
    VoiceOutcomeHandler,
    VoiceOutcomeServices,
)

MAX_CONCURRENT_INGESTIONS = 4


def build_handlers(
    client: TelegramClient,
    registry: GatewayRegistry,
    options: TelegramPollerOptions,
) -> tuple[PollerAccess, PollerCommands, MessageDispatcher]:
    access = PollerAccess(
        AccessServices(
            client,
            registry,
            options.membership,
            options.directory,
            options.identities,
            options.access,
        )
    )
    commands = PollerCommands(
        CommandServices(
            client,
            registry,
            options.secrets_link,
            options.referrals,
            options.bot_username,
        )
    )
    activity = IngestionActivity(client)
    slots = asyncio.Semaphore(MAX_CONCURRENT_INGESTIONS)
    material = MaterialDispatcher(MaterialServices(client, registry, commands))
    images = (
        ImageSubmitter(ImageServices(registry, options.media, slots, activity))
        if options.media is not None
        else None
    )
    voice_submitter = (
        VoiceSubmitter(
            VoiceServices(registry, options.media, slots, activity, options.voice_max_seconds)
        )
        if options.media is not None
        else None
    )
    voice = (
        VoiceOutcomeHandler(
            VoiceOutcomeServices(client, voice_submitter, material, options.voice_max_seconds)
        )
        if voice_submitter is not None
        else None
    )
    dispatcher = MessageDispatcher(
        DispatchServices(client, registry, commands, material, images, voice)
    )
    return access, commands, dispatcher
