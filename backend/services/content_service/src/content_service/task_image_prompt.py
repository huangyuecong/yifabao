"""Prompt policy for full-creation image stages and standalone image tasks only.

Video-project image candidates deliberately do not use this policy. These are
generation instructions, not pixel-preservation or guaranteed text correctness.
"""

LEGACY_REVISION_PREFIX = (
    "以当前参考图片为基础生成新版本，保留未明确要求修改的主体、构图和品牌元素。\n"
    "本次必须执行的修改要求："
)


def task_image_prompt_parts(
    *,
    instruction: str,
    subject: str,
    scene: str,
    style: str,
    ratio: str,
    reference_count: int,
    product_reference_indices: list[int],
    source_reference_index: int | None,
) -> list[str]:
    """Use one-based indices from the actual deduplicated provider input."""
    instruction = instruction.strip()
    subject = subject.strip()
    editing = source_reference_index is not None
    parts: list[str] = []
    if instruction:
        label = "本次图片修改要求（优先执行）" if editing else "本次图片生成要求"
        parts.append(f"{label}：{instruction}")

    products = sorted(set(product_reference_indices))
    product_only = [index for index in products if index != source_reference_index]
    if editing:
        parts.append(
            f"参考图{source_reference_index}是本次待修改画面；"
            "在这张画面上执行本次修改，不要把其他产品参考图当作待修改画面"
        )
    if product_only:
        labels = "、".join(f"参考图{index}" for index in product_only)
        parts.append(f"{labels}仅用于核对产品外观、包装和标签，不是要照搬的白底或原拍摄背景")
    elif products:
        parts.append("待修改画面同时作为产品外观参考；修改场景不代表可以改写品牌标签")
    other_indices = [
        index
        for index in range(1, reference_count + 1)
        if index not in products and index != source_reference_index
    ]
    if other_indices:
        labels = "、".join(f"参考图{index}" for index in other_indices)
        parts.append(f"{labels}是补充视觉参考，仅按用户要求借鉴，不得覆盖本次修改目标")

    if editing:
        parts.append(
            "只保留本次没有要求改变的部分；用户明确要求修改的背景、构图、材质或光线"
            "必须按要求改变，不得用默认保留规则抵消修改。"
            "当要求换背景时按指定目标更换；未指定新背景时，在原任务题材内选择明显不同的背景，"
            "不要只微调亮度或颜色代替换背景。用户明确要求保持的部分仍须保持"
        )
    if products:
        parts.extend(
            [
                (
                    "保持瓶型、包装结构、品牌 Logo 与标签文字一致，以产品原图为准，"
                    "不要继承待修改成图中已有的错字或变形。标签按参考图逐字核对，"
                    "不新增、臆造、替换文字或把文字当装饰纹理；保留可辨识字形，避免模糊和拉伸"
                ),
                (
                    "产品应按用户指定的画面风格自然融入场景：匹配真实尺寸、透视、环境色温、"
                    "光照方向与景深，调整接触阴影、材质反光和必要的遮挡关系，"
                    "避免悬浮、白边和生硬贴图感。"
                    "在用户要求的构图内保留足够的产品细节，不靠夸大产品比例营造真实感"
                ),
            ]
        )

    if scene:
        parts.append(f"{'场景参考（本次修改优先）' if editing else '场景'}：{scene}")
    if style:
        parts.append(f"{'风格参考（本次修改优先）' if editing else '风格'}：{style}")
    if ratio and ratio.lower() not in {"adaptive", "auto"}:
        parts.append(f"画面比例：{ratio}")
    # Older open browser tabs may still send the former frontend wrapper. Strip
    # only that exact wrapper; never remove a user's own preservation request.
    # Keep a long legacy subject after the explicit instruction and settings so
    # it cannot displace those fields when the final prompt budget is applied.
    if subject and subject not in {instruction, f"{LEGACY_REVISION_PREFIX}{instruction}"}:
        label = "补充画面描述（与本次修改冲突时以本次修改为准）" if editing else "图片主题"
        parts.append(f"{label}：{subject}")
    return parts
