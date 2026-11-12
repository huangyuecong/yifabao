from content_service.task_image_prompt import LEGACY_REVISION_PREFIX, task_image_prompt_parts


def prompt(**overrides):
    arguments = {
        "instruction": "",
        "subject": "新春团圆饭局",
        "scene": "生活方式",
        "style": "简约高级",
        "ratio": "adaptive",
        "reference_count": 0,
        "product_reference_indices": [],
        "source_reference_index": None,
    }
    arguments.update(overrides)
    return "；".join(task_image_prompt_parts(**arguments))


def test_edit_instruction_precedes_reference_roles_and_old_scene():
    instruction = "保持人物和产品的位置，背景改成夜间宴会厅，产品光线自然一些"
    value = prompt(
        instruction=instruction,
        subject=instruction,
        source_reference_index=3,
        reference_count=4,
        product_reference_indices=[1, 2, 1],
    )
    assert value.startswith(f"本次图片修改要求（优先执行）：{instruction}")
    assert value.count(instruction) == 1
    assert "参考图3是本次待修改画面" in value
    assert "参考图1、参考图2仅用于核对产品外观" in value
    assert "参考图4是补充视觉参考" in value
    assert "接触阴影" in value and "色温" in value
    assert "不要继承待修改成图中已有的错字或变形" in value
    assert "场景参考（本次修改优先）：生活方式" in value
    assert "画面比例：adaptive" not in value


def test_only_exact_legacy_wrapper_is_removed_not_user_preservation_request():
    instruction = "保留原来的主体、构图和品牌元素，只修改桌面倒影"
    value = prompt(
        instruction=instruction,
        subject=f"{LEGACY_REVISION_PREFIX}{instruction}",
        source_reference_index=1,
        reference_count=1,
    )
    assert value.count(instruction) == 1
    assert LEGACY_REVISION_PREFIX not in value
    assert "用户明确要求保持的部分仍须保持" in value
    custom_subject = "保留原来的主体、构图和品牌元素，同时补充灯光细节"
    custom = prompt(
        instruction=instruction,
        subject=custom_subject,
        source_reference_index=1,
        reference_count=1,
    )
    assert custom_subject in custom


def test_generation_without_references_does_not_claim_product_or_source_images():
    value = prompt(instruction="水彩山水画", ratio="16:9")
    assert value.startswith("本次图片生成要求：水彩山水画")
    assert "参考图" not in value
    assert "本次待修改画面" not in value
    assert "接触阴影" not in value
    assert "图片主题：新春团圆饭局" in value
    assert "风格：简约高级" in value
    assert "画面比例：16:9" in value


def test_product_generation_has_product_roles_but_does_not_invent_an_edit_target():
    value = prompt(reference_count=2, product_reference_indices=[1, 2])
    assert "参考图1、参考图2仅用于核对产品外观" in value
    assert "保持瓶型、包装结构、品牌 Logo 与标签文字一致" in value
    assert "本次待修改画面" not in value
    assert "当要求换背景时" not in value


def test_identical_product_and_edit_source_can_share_one_input_image():
    value = prompt(
        instruction="更换背景",
        reference_count=1,
        product_reference_indices=[1, 1],
        source_reference_index=1,
    )
    assert "参考图1是本次待修改画面" in value
    assert "待修改画面同时作为产品外观参考" in value
    assert "参考图1仅用于" not in value
    assert "参考图2" not in value
