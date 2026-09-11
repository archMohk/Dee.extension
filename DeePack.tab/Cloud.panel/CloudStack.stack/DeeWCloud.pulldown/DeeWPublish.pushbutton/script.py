# -*- coding: utf-8 -*-
"""DeeW.Publish (DeeW.Cloud)
Placeholder - not implemented yet. Shows the shared "Coming Soon"
dialog (lib/deew_coming_soon.py) rather than duplicating a one-off
dialog per tool. Will be built out to reuse the same DeeW.Cloud
services (deew_cloud_service, deew_document_manager, deew_model_scanner,
deew_failure_handler, deew_progress_service, deew_report_generator,
deew_settings, deew_logger) as DeeW.Sharing and DeeW.Batch Save to
Cloud, per the package's shared architecture.
"""
import deew_coming_soon
import dee_telemetry
dee_telemetry.check_access("DeeWPublish")


deew_coming_soon.show_coming_soon(
    "DeeW.Publish",
    "Will publish ACC/BIM360 cloud models for review, reusing DeeW.Cloud's shared cloud_service and model_scanner.")
