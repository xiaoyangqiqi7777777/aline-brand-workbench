from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from backend.agents.schemas.brand_spec import BrandSpec
from backend.agents.schemas.directions import DirectionOutput
from backend.agents.schemas.intake import IntakeOutput
from backend.agents.schemas.ip import IPOutput
from backend.agents.schemas.logo import LogoOutput
from backend.agents.schemas.materials import MaterialOutput
from backend.agents.schemas.proposal import ProposalOutput, ProposalSectionType
from backend.agents.schemas.review import ReviewOutput
from backend.agents.schemas.vi import VIOutput
from backend.agents.testing import InMemoryArtifactWriter, InMemoryInvocationRecorder
from backend.agents.workflow import build_brand_workflow
from backend.providers.models.fake import FakeImageModelProvider, FakeTextModelProvider


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _version(stage: str):
    return uuid5(NAMESPACE_URL, f"test-version:{stage}")


def _resume(workflow, config: dict, payload: dict):
    return workflow.invoke(Command(resume=payload), config=config)


def _complete_spec() -> BrandSpec:
    return BrandSpec(
        project_name="云山咖啡",
        industry="精品咖啡",
        brand_background="面向城市通勤者的社区精品咖啡品牌。",
        target_audiences=["25–35 岁城市通勤者"],
        price_positioning="中高端日常消费",
        brand_personality=["可靠", "温暖"],
        style_keywords=["现代", "自然", "克制"],
        prohibited_elements=["动物形象", "大面积红色"],
    )


def test_complete_information_reaches_direction_confirmation(workflow) -> None:
    result = workflow.invoke(
        {
            "project_id": "project-complete",
            "brand_spec": _complete_spec().model_dump(mode="json"),
            "status": "INTAKE",
        },
        config=_config("thread-complete"),
    )

    assert result["status"] == "WAITING_USER"
    direction_output = DirectionOutput.model_validate(result["direction_output"])
    assert len(direction_output.directions) == 3
    assert len({item.preview_asset_id for item in direction_output.directions}) == 3
    assert result["__interrupt__"][0].value["kind"] == "direction_decision"


def test_sparse_information_interrupts_and_resumes_from_checkpoint(workflow) -> None:
    config = _config("thread-sparse")
    first_result = workflow.invoke(
        {
            "project_id": "project-sparse",
            "brand_spec": BrandSpec(project_name="新品牌").model_dump(mode="json"),
            "status": "INTAKE",
        },
        config=config,
    )

    assert first_result["status"] == "NEEDS_INPUT"
    intake_output = IntakeOutput.model_validate(first_result["intake_output"])
    assert {question.field_path for question in intake_output.questions} == {
        "industry",
        "brand_background",
        "target_audiences",
        "style_keywords",
    }
    assert first_result["__interrupt__"][0].value["kind"] == "intake_questions"

    resumed = workflow.invoke(
        Command(
            resume={
                "answers": [
                    {"field_path": "industry", "value": "茶饮"},
                    {
                        "field_path": "brand_background",
                        "value": "提供当代东方风味的城市茶饮。",
                    },
                    {
                        "field_path": "target_audiences",
                        "value": ["年轻城市消费者"],
                    },
                    {
                        "field_path": "style_keywords",
                        "value": ["当代", "东方", "清爽"],
                    },
                ]
            }
        ),
        config=config,
    )

    assert resumed["status"] == "WAITING_USER"
    assert len(DirectionOutput.model_validate(resumed["direction_output"]).directions) == 3


def test_conflicting_information_is_reported_instead_of_invented(workflow) -> None:
    conflicting = _complete_spec().model_copy(
        update={
            "price_positioning": "极低价儿童市场",
            "style_keywords": ["高端奢华", "冷峻"],
        }
    )
    result = workflow.invoke(
        {
            "project_id": "project-conflict",
            "brand_spec": conflicting.model_dump(mode="json"),
            "status": "INTAKE",
        },
        config=_config("thread-conflict"),
    )

    assert result["status"] == "NEEDS_INPUT"
    intake_output = IntakeOutput.model_validate(result["intake_output"])
    assert [conflict.code for conflict in intake_output.conflicts] == ["POSITIONING_STYLE_CONFLICT"]
    assert intake_output.questions[0].field_path == "style_keywords"


def test_full_workflow_can_skip_ip_and_reach_export_ready(workflow) -> None:
    config = _config("thread-skip-ip")
    result = workflow.invoke(
        {
            "project_id": "project-skip-ip",
            "brand_spec": _complete_spec().model_dump(mode="json"),
            "status": "INTAKE",
        },
        config=config,
    )
    assert result["__interrupt__"][0].value["kind"] == "direction_decision"

    result = _resume(
        workflow,
        config,
        {
            "version_id": str(_version("directions")),
            "selected_item_id": "direction-clear",
        },
    )
    assert result["__interrupt__"][0].value["kind"] == "logo_decision"

    result = _resume(
        workflow,
        config,
        {
            "version_id": str(_version("logo")),
            "selected_item_id": "logo-wordmark",
        },
    )
    assert result["__interrupt__"][0].value["kind"] == "vi_decision"

    result = _resume(
        workflow,
        config,
        {"version_id": str(_version("vi")), "confirmed": True},
    )
    assert result["__interrupt__"][0].value["kind"] == "ip_choice"

    result = _resume(workflow, config, {"action": "SKIP"})
    assert result["ip_skipped"] is True
    assert result["__interrupt__"][0].value["kind"] == "material_decision"

    result = _resume(
        workflow,
        config,
        {"version_id": str(_version("materials")), "confirmed": True},
    )
    assert result["__interrupt__"][0].value["kind"] == "review_decision"

    result = _resume(
        workflow,
        config,
        {
            "version_id": str(_version("review")),
            "proceed": True,
            "accepted_issue_ids": [],
        },
    )
    proposal = ProposalOutput.model_validate(result["proposal_output"])
    assert ProposalSectionType.IP not in {section.type for section in proposal.sections}
    assert result["__interrupt__"][0].value["kind"] == "proposal_decision"

    result = _resume(
        workflow,
        config,
        {"version_id": str(_version("proposal")), "confirmed": True},
    )
    assert result["status"] == "EXPORT_READY"
    assert set(result["selected_version_ids"]) == {
        "DIRECTIONS",
        "LOGO",
        "VI",
        "MATERIALS",
        "REVIEW",
        "PROPOSAL",
    }


def test_full_workflow_can_generate_ip(workflow) -> None:
    config = _config("thread-with-ip")
    workflow.invoke(
        {
            "project_id": "project-with-ip",
            "brand_spec": _complete_spec().model_dump(mode="json"),
            "status": "INTAKE",
        },
        config=config,
    )
    _resume(
        workflow,
        config,
        {
            "version_id": str(_version("directions-ip")),
            "selected_item_id": "direction-warm",
        },
    )
    _resume(
        workflow,
        config,
        {
            "version_id": str(_version("logo-ip")),
            "selected_item_id": "logo-symbol",
        },
    )
    _resume(
        workflow,
        config,
        {"version_id": str(_version("vi-ip")), "confirmed": True},
    )
    result = _resume(workflow, config, {"action": "GENERATE"})
    assert result["__interrupt__"][0].value["kind"] == "ip_decision"
    IPOutput.model_validate(result["ip_output"])

    result = _resume(
        workflow,
        config,
        {"version_id": str(_version("ip")), "confirmed": True},
    )
    assert result["__interrupt__"][0].value["kind"] == "material_decision"
    result = _resume(
        workflow,
        config,
        {"version_id": str(_version("materials-ip")), "confirmed": True},
    )
    result = _resume(
        workflow,
        config,
        {
            "version_id": str(_version("review-ip")),
            "proceed": True,
            "accepted_issue_ids": [],
        },
    )
    proposal = ProposalOutput.model_validate(result["proposal_output"])
    assert ProposalSectionType.IP in {section.type for section in proposal.sections}


def test_graph_can_resume_after_worker_rebuild() -> None:
    checkpointer = InMemorySaver()
    artifact_writer = InMemoryArtifactWriter()
    invocation_recorder = InMemoryInvocationRecorder()

    def build():
        return build_brand_workflow(
            text_provider=FakeTextModelProvider(),
            image_provider=FakeImageModelProvider(),
            artifact_writer=artifact_writer,
            invocation_recorder=invocation_recorder,
            checkpointer=checkpointer,
        )

    config = _config("thread-restart")
    first_worker = build()
    interrupted = first_worker.invoke(
        {
            "project_id": "project-restart",
            "brand_spec": _complete_spec().model_dump(mode="json"),
            "status": "INTAKE",
        },
        config=config,
    )
    assert interrupted["__interrupt__"][0].value["kind"] == "direction_decision"

    rebuilt_worker = build()
    resumed = rebuilt_worker.invoke(
        Command(
            resume={
                "version_id": str(_version("restart-directions")),
                "selected_item_id": "direction-clear",
            }
        ),
        config=config,
    )

    assert resumed["__interrupt__"][0].value["kind"] == "logo_decision"
    assert resumed["selected_direction_id"] == "direction-clear"
    assert any(record.capability.value == "LOGO" for record in invocation_recorder.records)


def test_logo_selection_resumes_after_worker_rebuild_and_reaches_vi_decision() -> None:
    checkpointer = InMemorySaver()
    artifact_writer = InMemoryArtifactWriter()
    invocation_recorder = InMemoryInvocationRecorder()

    def build():
        return build_brand_workflow(
            text_provider=FakeTextModelProvider(),
            image_provider=FakeImageModelProvider(),
            artifact_writer=artifact_writer,
            invocation_recorder=invocation_recorder,
            checkpointer=checkpointer,
        )

    config = _config("thread-logo-to-vi-restart")
    first_worker = build()
    first_worker.invoke(
        {
            "project_id": "project-logo-to-vi-restart",
            "brand_spec": _complete_spec().model_dump(mode="json"),
            "status": "INTAKE",
        },
        config=config,
    )
    waiting_for_logo = _resume(
        first_worker,
        config,
        {
            "version_id": str(_version("restart-directions-for-vi")),
            "selected_item_id": "direction-clear",
        },
    )
    logo_output = LogoOutput.model_validate(waiting_for_logo["logo_output"])
    selected_logo = next(
        concept for concept in logo_output.concepts if concept.id == "logo-wordmark"
    )
    assert waiting_for_logo["__interrupt__"][0].value["kind"] == "logo_decision"

    rebuilt_worker = build()
    waiting_for_vi = _resume(
        rebuilt_worker,
        config,
        {
            "version_id": str(_version("restart-logo")),
            "selected_item_id": selected_logo.id,
        },
    )
    vi_output = VIOutput.model_validate(waiting_for_vi["vi_output"])

    assert waiting_for_vi["__interrupt__"][0].value["kind"] == "vi_decision"
    assert waiting_for_vi["selected_logo_id"] == selected_logo.id
    assert waiting_for_vi["selected_version_ids"]["LOGO"] == str(_version("restart-logo"))
    assert vi_output.source_logo_asset_id == selected_logo.preview_asset_id
    assert sum(record.capability.value == "VI" for record in invocation_recorder.records) == 1


@pytest.mark.parametrize(
    ("action", "expected_interrupt", "expects_ip"),
    [
        ("GENERATE", "ip_decision", True),
        ("SKIP", "material_decision", False),
    ],
)
def test_vi_confirmation_and_ip_choice_resume_after_worker_rebuild(
    action: str,
    expected_interrupt: str,
    expects_ip: bool,
) -> None:
    checkpointer = InMemorySaver()
    artifact_writer = InMemoryArtifactWriter()
    invocation_recorder = InMemoryInvocationRecorder()

    def build():
        return build_brand_workflow(
            text_provider=FakeTextModelProvider(),
            image_provider=FakeImageModelProvider(),
            artifact_writer=artifact_writer,
            invocation_recorder=invocation_recorder,
            checkpointer=checkpointer,
        )

    config = _config(f"thread-vi-ip-{action.lower()}")
    first_worker = build()
    first_worker.invoke(
        {
            "project_id": f"project-vi-ip-{action.lower()}",
            "brand_spec": _complete_spec().model_dump(mode="json"),
            "status": "INTAKE",
        },
        config=config,
    )
    _resume(
        first_worker,
        config,
        {
            "version_id": str(_version(f"vi-ip-directions-{action}")),
            "selected_item_id": "direction-clear",
        },
    )
    waiting_for_vi = _resume(
        first_worker,
        config,
        {
            "version_id": str(_version(f"vi-ip-logo-{action}")),
            "selected_item_id": "logo-wordmark",
        },
    )
    assert waiting_for_vi["__interrupt__"][0].value["kind"] == "vi_decision"

    confirmation_worker = build()
    waiting_for_ip_choice = _resume(
        confirmation_worker,
        config,
        {
            "version_id": str(_version(f"vi-ip-vi-{action}")),
            "confirmed": True,
        },
    )
    assert waiting_for_ip_choice["__interrupt__"][0].value["kind"] == "ip_choice"
    assert waiting_for_ip_choice["selected_version_ids"]["VI"] == str(
        _version(f"vi-ip-vi-{action}")
    )

    choice_worker = build()
    resumed = _resume(choice_worker, config, {"action": action})
    ip_invocations = [
        record for record in invocation_recorder.records if record.capability.value == "IP"
    ]

    assert resumed["__interrupt__"][0].value["kind"] == expected_interrupt
    assert resumed["ip_skipped"] is (not expects_ip)
    assert len(ip_invocations) == (2 if expects_ip else 0)
    assert sum(record.image_count for record in ip_invocations) == (1 if expects_ip else 0)
    if expects_ip:
        IPOutput.model_validate(resumed["ip_output"])
    else:
        assert "ip_output" not in resumed
        material_output = MaterialOutput.model_validate(resumed["material_output"])
        vi_output = VIOutput.model_validate(waiting_for_ip_choice["vi_output"])
        assert all(
            scene.used_asset_ids == [vi_output.source_logo_asset_id]
            for scene in material_output.scenes
        )


def test_ip_confirmation_resumes_after_worker_rebuild_and_generates_materials() -> None:
    checkpointer = InMemorySaver()
    artifact_writer = InMemoryArtifactWriter()
    invocation_recorder = InMemoryInvocationRecorder()

    def build():
        return build_brand_workflow(
            text_provider=FakeTextModelProvider(),
            image_provider=FakeImageModelProvider(),
            artifact_writer=artifact_writer,
            invocation_recorder=invocation_recorder,
            checkpointer=checkpointer,
        )

    config = _config("thread-ip-to-materials-restart")
    first_worker = build()
    first_worker.invoke(
        {
            "project_id": "project-ip-to-materials-restart",
            "brand_spec": _complete_spec().model_dump(mode="json"),
            "status": "INTAKE",
        },
        config=config,
    )
    _resume(
        first_worker,
        config,
        {
            "version_id": str(_version("ip-materials-directions")),
            "selected_item_id": "direction-clear",
        },
    )
    _resume(
        first_worker,
        config,
        {
            "version_id": str(_version("ip-materials-logo")),
            "selected_item_id": "logo-wordmark",
        },
    )
    _resume(
        first_worker,
        config,
        {
            "version_id": str(_version("ip-materials-vi")),
            "confirmed": True,
        },
    )
    waiting_for_ip = _resume(first_worker, config, {"action": "GENERATE"})
    assert waiting_for_ip["__interrupt__"][0].value["kind"] == "ip_decision"
    vi_output = VIOutput.model_validate(waiting_for_ip["vi_output"])
    ip_output = IPOutput.model_validate(waiting_for_ip["ip_output"])

    rebuilt_worker = build()
    waiting_for_materials = _resume(
        rebuilt_worker,
        config,
        {
            "version_id": str(_version("ip-materials-ip")),
            "confirmed": True,
        },
    )
    material_output = MaterialOutput.model_validate(waiting_for_materials["material_output"])
    material_invocations = [
        record for record in invocation_recorder.records if record.capability.value == "MATERIALS"
    ]
    expected_references = {
        vi_output.source_logo_asset_id,
        ip_output.preview_asset_id,
    }

    assert waiting_for_materials["__interrupt__"][0].value["kind"] == "material_decision"
    assert waiting_for_materials["selected_version_ids"]["IP"] == str(_version("ip-materials-ip"))
    assert len(material_output.scenes) == 2
    assert all(set(scene.used_asset_ids) == expected_references for scene in material_output.scenes)
    assert len(material_invocations) == 3
    assert sum(record.image_count for record in material_invocations) == 2


def test_material_confirmation_resumes_after_worker_rebuild_and_generates_review() -> None:
    checkpointer = InMemorySaver()
    artifact_writer = InMemoryArtifactWriter()
    invocation_recorder = InMemoryInvocationRecorder()

    def build():
        return build_brand_workflow(
            text_provider=FakeTextModelProvider(),
            image_provider=FakeImageModelProvider(),
            artifact_writer=artifact_writer,
            invocation_recorder=invocation_recorder,
            checkpointer=checkpointer,
        )

    config = _config("thread-materials-to-review-restart")
    first_worker = build()
    first_worker.invoke(
        {
            "project_id": "project-materials-to-review-restart",
            "brand_spec": _complete_spec().model_dump(mode="json"),
            "status": "INTAKE",
        },
        config=config,
    )
    _resume(
        first_worker,
        config,
        {
            "version_id": str(_version("materials-review-directions")),
            "selected_item_id": "direction-clear",
        },
    )
    _resume(
        first_worker,
        config,
        {
            "version_id": str(_version("materials-review-logo")),
            "selected_item_id": "logo-wordmark",
        },
    )
    _resume(
        first_worker,
        config,
        {
            "version_id": str(_version("materials-review-vi")),
            "confirmed": True,
        },
    )
    waiting_for_materials = _resume(first_worker, config, {"action": "SKIP"})
    assert waiting_for_materials["__interrupt__"][0].value["kind"] == "material_decision"
    material_output = MaterialOutput.model_validate(waiting_for_materials["material_output"])

    rebuilt_worker = build()
    waiting_for_review = _resume(
        rebuilt_worker,
        config,
        {
            "version_id": str(_version("materials-review-materials")),
            "confirmed": True,
        },
    )
    review_output = ReviewOutput.model_validate(waiting_for_review["review_output"])
    review_invocations = [
        record for record in invocation_recorder.records if record.capability.value == "REVIEW"
    ]

    assert waiting_for_review["__interrupt__"][0].value["kind"] == "review_decision"
    assert waiting_for_review["selected_version_ids"]["MATERIALS"] == str(
        _version("materials-review-materials")
    )
    assert waiting_for_review["review_output"]["pass"] is True
    assert "passed" not in waiting_for_review["review_output"]
    assert review_output.passed is True
    assert review_output.issues == []
    assert len(review_invocations) == 1
    assert review_invocations[0].image_count == 0
    assert len({scene.preview_asset_id for scene in material_output.scenes}) == 2


def test_review_decision_resumes_after_worker_rebuild_and_generates_proposal() -> None:
    checkpointer = InMemorySaver()
    artifact_writer = InMemoryArtifactWriter()
    invocation_recorder = InMemoryInvocationRecorder()

    def build():
        return build_brand_workflow(
            text_provider=FakeTextModelProvider(),
            image_provider=FakeImageModelProvider(),
            artifact_writer=artifact_writer,
            invocation_recorder=invocation_recorder,
            checkpointer=checkpointer,
        )

    config = _config("thread-review-to-proposal-restart")
    first_worker = build()
    first_worker.invoke(
        {
            "project_id": "project-review-to-proposal-restart",
            "brand_spec": _complete_spec().model_dump(mode="json"),
            "status": "INTAKE",
        },
        config=config,
    )
    _resume(
        first_worker,
        config,
        {
            "version_id": str(_version("review-proposal-directions")),
            "selected_item_id": "direction-clear",
        },
    )
    _resume(
        first_worker,
        config,
        {
            "version_id": str(_version("review-proposal-logo")),
            "selected_item_id": "logo-wordmark",
        },
    )
    _resume(
        first_worker,
        config,
        {
            "version_id": str(_version("review-proposal-vi")),
            "confirmed": True,
        },
    )
    waiting_for_materials = _resume(first_worker, config, {"action": "SKIP"})
    waiting_for_review = _resume(
        first_worker,
        config,
        {
            "version_id": str(_version("review-proposal-materials")),
            "confirmed": True,
        },
    )
    assert waiting_for_review["__interrupt__"][0].value["kind"] == "review_decision"
    direction_output = DirectionOutput.model_validate(waiting_for_review["direction_output"])
    logo_output = LogoOutput.model_validate(waiting_for_review["logo_output"])
    material_output = MaterialOutput.model_validate(waiting_for_materials["material_output"])
    selected_direction = next(
        item for item in direction_output.directions if item.id == "direction-clear"
    )
    selected_logo = next(item for item in logo_output.concepts if item.id == "logo-wordmark")
    artifact_count_before_proposal = len(artifact_writer.items)

    rebuilt_worker = build()
    waiting_for_proposal = _resume(
        rebuilt_worker,
        config,
        {
            "version_id": str(_version("review-proposal-review")),
            "proceed": True,
            "accepted_issue_ids": [],
        },
    )
    proposal = ProposalOutput.model_validate(waiting_for_proposal["proposal_output"])
    proposal_invocations = [
        record for record in invocation_recorder.records if record.capability.value == "PROPOSAL"
    ]
    sections = {section.type: section for section in proposal.sections}
    expected_asset_refs = {
        selected_direction.preview_asset_id,
        selected_logo.preview_asset_id,
        *(scene.preview_asset_id for scene in material_output.scenes),
    }

    assert waiting_for_proposal["__interrupt__"][0].value["kind"] == "proposal_decision"
    assert waiting_for_proposal["selected_version_ids"]["REVIEW"] == str(
        _version("review-proposal-review")
    )
    assert [section.type for section in proposal.sections] == [
        ProposalSectionType.BRIEF,
        ProposalSectionType.DIRECTION,
        ProposalSectionType.LOGO,
        ProposalSectionType.VI,
        ProposalSectionType.MATERIALS,
        ProposalSectionType.REVIEW_SUMMARY,
    ]
    assert sections[ProposalSectionType.BRIEF].version_id == _version("review-proposal-directions")
    assert sections[ProposalSectionType.DIRECTION].version_id == _version(
        "review-proposal-directions"
    )
    assert sections[ProposalSectionType.LOGO].version_id == _version("review-proposal-logo")
    assert sections[ProposalSectionType.VI].version_id == _version("review-proposal-vi")
    assert sections[ProposalSectionType.MATERIALS].version_id == _version(
        "review-proposal-materials"
    )
    assert sections[ProposalSectionType.REVIEW_SUMMARY].version_id == _version(
        "review-proposal-review"
    )
    assert set(proposal.asset_refs) == expected_asset_refs
    assert ProposalSectionType.IP not in sections
    assert len(proposal_invocations) == 1
    assert proposal_invocations[0].image_count == 0
    assert len(artifact_writer.items) == artifact_count_before_proposal
