import time

from django.core.management.base import BaseCommand

from ai_pipeline.services import run_worker_once


class Command(BaseCommand):
    help = "Process queued Fine PAPERs AI pipeline operations."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Process at most one queued operation.")
        parser.add_argument("--poll-interval", type=float, default=2.0)

    def handle(self, *args, **options):
        poll_interval = max(0.25, options["poll_interval"])
        if options["once"]:
            if not run_worker_once():
                self.stdout.write("No queued AI operation.")
            return
        self.stdout.write("AI worker started.")
        while True:
            if not run_worker_once():
                time.sleep(poll_interval)
