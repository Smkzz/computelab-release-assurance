"""Compatibility exports for project setup and bounded paired qualification.

Implementation modules keep project mutation, evaluation, validation, decisions,
and checkpoint execution separate. Existing runner imports remain available.
"""

from __future__ import annotations

import os as os
import platform as platform
import re as re
import sys as sys
import uuid as uuid
from contextlib import contextmanager as contextmanager
from pathlib import Path as Path
from typing import Any as Any
from typing import Iterator as Iterator

from ._decision import _metric_decision as _metric_decision
from ._decision import _qualification_decision as _qualification_decision
from ._decision import aggregate as aggregate
from ._decision import decide as decide
from ._evaluation import _evaluate_json as _evaluate_json
from ._evaluation import _evaluate_tools as _evaluate_tools
from ._evaluation import _jobs as _jobs
from ._evaluation import _match as _match
from ._evaluation import _request_payload as _request_payload
from ._evaluation import evaluate_case as evaluate_case
from ._project import _assert_no_active as _assert_no_active
from ._project import _load as _load
from ._project import _lock as _lock
from ._project import define_contract as define_contract
from ._project import initialize_project as initialize_project
from ._project import project_root as project_root
from ._project import register_deployment as register_deployment
from ._qualification import POLICY as POLICY
from ._qualification import _build_receipt as _build_receipt
from ._qualification import _complete_run as _complete_run
from ._qualification import _execute_jobs as _execute_jobs
from ._qualification import _prepare_run as _prepare_run
from ._qualification import _run_dir as _run_dir
from ._qualification import _runtime as _runtime
from ._qualification import load_latest_receipt as load_latest_receipt
from ._qualification import qualify as _qualify
from ._results import _validate_result_metrics as _validate_result_metrics
from ._results import _validate_result_observability as _validate_result_observability
from ._results import _validate_result_status as _validate_result_status
from ._results import validate_results as validate_results
from .models import DEPLOYMENT_SCHEMA as DEPLOYMENT_SCHEMA
from .models import MANIFEST_SCHEMA as MANIFEST_SCHEMA
from .models import MAX_JOBS as MAX_JOBS
from .models import METRIC_LIMITS as METRIC_LIMITS
from .models import PRODUCT_VERSION as PRODUCT_VERSION
from .models import PROJECT_SCHEMA as PROJECT_SCHEMA
from .models import RECEIPT_SCHEMA as RECEIPT_SCHEMA
from .models import EvidenceValidationError as EvidenceValidationError
from .models import ReleaseAssuranceError as ReleaseAssuranceError
from .models import canonical_json as canonical_json
from .models import contract_hash as contract_hash
from .models import default_contract as default_contract
from .models import default_environment as default_environment
from .models import deployment_fingerprint as deployment_fingerprint
from .models import environment_fingerprint as environment_fingerprint
from .models import finite_number as finite_number
from .models import json_schema_errors as json_schema_errors
from .models import percentile as percentile
from .models import project_hash as project_hash
from .models import public_endpoint as public_endpoint
from .models import read_json as read_json
from .models import reject_links as reject_links
from .models import safe_path as safe_path
from .models import sha256_file as sha256_file
from .models import sha256_json as sha256_json
from .models import strict_json as strict_json
from .models import utc_now as utc_now
from .models import validate_contract as validate_contract
from .models import validate_deployment as validate_deployment
from .models import validate_endpoint as validate_endpoint
from .models import validate_project as validate_project
from .models import write_json as write_json
from .models import write_text as write_text
from .transport import CompletionResult as CompletionResult
from .transport import OpenAICompatibleClient as OpenAICompatibleClient


def qualify(
    project_dir: Path,
    *,
    repeats: int | None = None,
    timeout_s: float = 30.0,
    resume: bool = False,
    stop_after: int | None = None,
) -> dict[str, Any]:
    """Run qualification with the compatibility module's current request budget."""
    return _qualify(
        project_dir,
        repeats=repeats,
        timeout_s=timeout_s,
        resume=resume,
        stop_after=stop_after,
        max_jobs=MAX_JOBS,
    )
