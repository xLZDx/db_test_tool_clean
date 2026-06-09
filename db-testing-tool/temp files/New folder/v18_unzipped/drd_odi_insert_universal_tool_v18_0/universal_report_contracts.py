#!/usr/bin/env python3
"""Stable public enums for v18.0."""
from enum import Enum

class ProcessStatus(str, Enum):
    ARTIFACTS_GENERATED = "ARTIFACTS_GENERATED"
    FAILED_INPUT_CONTRACT = "FAILED_INPUT_CONTRACT"
    FAILED_ENGINE = "FAILED_ENGINE"
    FAILED_REPORT_GENERATION = "FAILED_REPORT_GENERATION"

class BusinessStatus(str, Enum):
    SOLVED_OR_NO_ACTIVE_REVIEW_ROWS = "SOLVED_OR_NO_ACTIVE_REVIEW_ROWS"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    INSERT_BLOCKER = "INSERT_BLOCKER"
    NOT_EVALUATED = "NOT_EVALUATED"

class PipelineMode(str, Enum):
    API_FIRST = "api_first"

class ReportMode(str, Enum):
    API = "api"
    EXCEL = "excel"
    BOTH = "both"
