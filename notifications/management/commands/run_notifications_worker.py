import time

from django.core.management.base import BaseCommand

from notifications.worker import run_worker_once


class Command(BaseCommand):
    help = "Process queued Finepapers in-app notification events."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--poll-interval", type=float, default=2.0)

    def handle(self, *args, **options):
        poll_interval = max(0.25, options["poll_interval"])
        if options["once"]:
            if not run_worker_once():
                self.stdout.write("No queued notification event.")
            return
        self.stdout.write("Notification worker started.")
        while True:
            if not run_worker_once():
                time.sleep(poll_interval)
