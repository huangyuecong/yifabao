"""Product video prompts and on-screen text placement for short-video apps."""

from PIL import Image

from content_service import catalog_ai_video, catalog_video


def test_shot_prompts_vary_and_keep_the_product_unchanged():
    prompts = [catalog_ai_video.shot_prompt("雅酿酒·典藏2008", index, "突出礼盒") for index in range(3)]
    assert len(set(prompts)) == 3
    for prompt in prompts:
        assert prompt.startswith("雅酿酒·典藏2008。")
        assert catalog_ai_video.GUARD in prompt and catalog_ai_video.BOTTLE in prompt
        assert prompt.endswith("补充要求：突出礼盒")
    assert catalog_ai_video.BOTTLE not in catalog_ai_video.shot_prompt("真丝围巾", 0)


def test_title_sits_below_the_apps_top_bar(tmp_path):
    overlay = catalog_video._title_overlay("雅酿·典藏2008 礼盒装", tmp_path / "title.png")
    with Image.open(overlay) as image:
        alpha = image.getchannel("A")
        rows = [y for y in range(image.height) if alpha.crop((0, y, image.width, y + 1)).getextrema()[1] > 200]
    # The 关注/推荐 tabs cover roughly the top 12% of the screen.
    assert rows and min(rows) > catalog_video.HEIGHT * 0.12
    assert catalog_video.SUBTITLE_STYLE["margin_v"] > 48
