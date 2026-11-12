"""Measure what retrieval adds to batch copy generation (RAG on vs off).

For each sampled product that has an official knowledge entry, the real
configured model writes copy twice from identical inputs (product record +
the same photos): once without references and once with the retrieved
passages, exactly as batch generation does. Each version is then scored:

* detail coverage - distinctive clauses of the official material (slogan,
  story, design) that are not already in the product record, and that the copy
  reuses (a clause counts when any 4-character span of it appears);
* unsupported numbers - numbers in the copy (years, ABV, volumes, counts) found
  in neither the product record nor the official material, i.e. invented;
* blind judging (--judge) - the model compares the two versions in random order
  without knowing which used retrieval.

Writes docs/rag-evaluation.md and docs/rag-evaluation.json. Run from backend/:
  .venv\\Scripts\\python.exe scripts\\rag_eval.py --limit 10 --judge
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in ("services/content_service/src", "packages/agent_core/src", "packages/platform_auth/src"):
    sys.path.insert(0, str(ROOT / path))

SPLIT = re.compile(r"[，。；！？、,.;!?\s:：“”\"'「」『』（）()《》\-—…·]+")
NUMBER = re.compile(r"\d+(?:\.\d+)?")


def load_env() -> None:
    from dotenv import dotenv_values

    os.chdir(ROOT)
    for key, value in dotenv_values(ROOT / ".env").items():
        if value is not None:
            os.environ.setdefault(key, value)


def clauses(knowledge: str, record: str) -> list[str]:
    """Distinctive clauses of the official text that the product record does not already contain."""
    seen, result = set(), []
    for part in SPLIT.split(knowledge):
        part = part.strip()
        if len(part) >= 4 and part not in record and part not in seen:
            seen.add(part)
            result.append(part)
    return result


def covered(clause: str, text: str) -> bool:
    return any(clause[index:index + 4] in text for index in range(len(clause) - 3))


def unsupported_numbers(text: str, truth: str) -> list[str]:
    known = set(NUMBER.findall(truth))
    return sorted({number for number in NUMBER.findall(text) if number not in known})


def copy_text(draft: dict) -> str:
    return "\n".join(str(draft.get(key) or "") for key in ("title", "description", "listing_title",
                                                           "listing_description"))


def retry(call, attempts: int = 3, pause: float = 8.0):
    """Model calls can drop on a flaky network; retry before counting a product as failed."""
    from content_service.llm_adapters import ProviderCallError

    for attempt in range(attempts):
        try:
            return call()
        except ProviderCallError:
            if attempt == attempts - 1:
                raise
            time.sleep(pause * (attempt + 1))
    return None


def judge(settings, text_a: str, text_b: str, truth: str) -> dict:
    from content_service.chat_provider import call_chat

    prompt = (
        "你是电商内容质检员。下面是同一件商品的两版宣传文案，以及该商品的官方资料。"
        "请从两个维度比较：1) 具体度与辨识度：是否写出了这件商品独有的卖点、故事或设计，而不是空泛套话；"
        "2) 事实准确：是否只写了官方资料或商品信息能支持的内容，没有编造数字、奖项、功效。"
        "只输出 JSON：{\"specific\": \"A\"|\"B\"|\"tie\", \"accurate\": \"A\"|\"B\"|\"tie\", \"reason\": \"一句话\"}。\n\n"
        f"【官方资料与商品信息】\n{truth[:2500]}\n\n【文案 A】\n{text_a}\n\n【文案 B】\n{text_b}"
    )
    reply = call_chat(settings, {"model": settings.llm_model}, [{"role": "user", "content": prompt}])
    match = re.search(r"\{.*\}", reply, re.S)
    return json.loads(match.group(0)) if match else {"specific": "tie", "accurate": "tie", "reason": reply[:80]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--images", type=int, default=2, help="photos sent per product (same for both arms)")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--min-clauses", type=int, default=5,
                        help="skip products whose official text adds fewer distinctive clauses than this")
    args = parser.parse_args()
    load_env()

    from sqlalchemy import select, true

    from content_service import catalog_knowledge, db
    from content_service.automation_content import generate_draft
    from content_service.catalog_generation import _source_images, product_facts
    from content_service.catalog_media import new_work_dir
    from content_service.catalog_models import Product, ProductImage
    from content_service.models import SensitiveRule
    from content_service.provider_dependencies import get_runtime_settings

    settings = get_runtime_settings()
    rng = random.Random(args.seed)
    rows, started = [], time.monotonic()
    with db.SessionLocal() as session:
        rules = list(session.scalars(select(SensitiveRule.phrase).where(SensitiveRule.active == true())))
        with_images = set(session.scalars(select(ProductImage.product_id)).all())
        products = [product for product in session.scalars(
            select(Product).where(Product.active.is_(True)).order_by(Product.name)).all()
            if product.id in with_images]
        rng.shuffle(products)  # a seeded random sample, not just the first names alphabetically
        for product in products:
            if len(rows) >= args.limit:
                break
            specs = json.loads(product.specs_json or "[]")
            references = catalog_knowledge.references_for(session, product.owner_id, product.name,
                                                           product.category, specs)
            exact = [ref for ref in references if catalog_knowledge._same_product(ref["title"], product.name)]
            if not exact:
                continue
            record = product_facts(product)
            knowledge = "\n".join(ref["text"] for ref in exact)
            truth = record + "\n" + knowledge
            targets = clauses(knowledge, record)
            if len(targets) < args.min_clauses:
                continue
            directory = new_work_dir("rag-eval", product.id)
            try:
                sources = _source_images(session, product, directory)[: args.images]
                config = {"purpose": "creation", "platforms": [], "instructions": ""}
                base = {"name": product.name, "text": record, "category": product.category}
                result = {"product": product.name, "references": [ref["title"] for ref in references],
                          "clauses": len(targets)}
                for arm, doc in (("without", base), ("with", {**base, "references": references})):
                    try:
                        draft, model = retry(lambda doc=doc: generate_draft(config, doc, sources, rules, settings))
                        text = copy_text(draft)
                        result[arm] = {
                            "text": text, "model": model, "chars": len(text),
                            "covered": sum(covered(clause, text) for clause in targets),
                            "unsupported_numbers": unsupported_numbers(text, truth),
                        }
                    except Exception as exc:  # report, do not hide, a failed generation
                        result[arm] = {"error": str(exc)[:200]}
                if args.judge and "text" in result["without"] and "text" in result["with"]:
                    flipped = rng.random() < 0.5
                    first, second = ("with", "without") if flipped else ("without", "with")
                    verdict = retry(lambda: judge(settings, result[first]["text"], result[second]["text"], truth))
                    decode = {"A": first, "B": second, "tie": "tie"}
                    result["judge"] = {key: decode.get(verdict.get(key), "tie") for key in ("specific", "accurate")}
                    result["judge"]["reason"] = str(verdict.get("reason", ""))[:120]
                rows.append(result)
                print(f"[{len(rows)}/{args.limit}] {product.name}", flush=True)
            finally:
                shutil.rmtree(directory, ignore_errors=True)
    write_report(rows, settings, args, time.monotonic() - started)


def write_report(rows: list[dict], settings, args, seconds: float) -> None:
    done = [row for row in rows if "text" in row.get("without", {}) and "text" in row.get("with", {})]
    total_clauses = sum(row["clauses"] for row in done)

    def total(arm: str, key: str) -> int:
        return sum(row[arm][key] if key != "unsupported_numbers" else len(row[arm][key]) for row in done)

    def wins(key: str, arm: str) -> int:
        return sum(1 for row in done if row.get("judge", {}).get(key) == arm)

    lines = [
        "# 检索增强（RAG）对批量文案生成的效果评测",
        "",
        f"- 评测时间：{time.strftime('%Y-%m-%d %H:%M')}；文本模型：`{settings.llm_model}`；"
        f"样本：商品库中官方资料比商品信息至少多 {args.min_clauses} 条独特语句的商品，按固定随机种子 {args.seed} 抽取 {len(rows)} 个"
        f"（成功 {len(done)} 个，失败 {len(rows) - len(done)} 个）。",
        f"- 两组输入完全相同（商品信息 + 同样 {args.images} 张商品图），唯一区别是是否附带检索到的资料。耗时 {seconds / 60:.1f} 分钟。",
        "- 指标说明：**资料细节覆盖**＝官方资料中、商品信息里没有的独特语句（宣传语、产品故事、设计描述）被文案用到的条数；"
        "**无依据数字**＝文案中出现、但商品信息和官方资料里都没有的数字（年份、度数、容量、数量等），即编造的数据。",
        "",
        "## 汇总",
        "",
        "| 指标 | 不检索 | 检索增强 |",
        "| --- | --- | --- |",
        f"| 资料细节覆盖（共 {total_clauses} 条可用） | {total('without', 'covered')} | {total('with', 'covered')} |",
        f"| 无依据数字（越少越好） | {total('without', 'unsupported_numbers')} | {total('with', 'unsupported_numbers')} |",
        f"| 平均文案长度（字） | {total('without', 'chars') // max(len(done), 1)} | {total('with', 'chars') // max(len(done), 1)} |",
    ]
    if any("judge" in row for row in done):
        judged = sum(1 for row in done if "judge" in row)
        lines += [
            f"| 盲评·具体度胜出（{judged} 组） | {wins('specific', 'without')} | {wins('specific', 'with')} |",
            f"| 盲评·事实准确胜出（{judged} 组） | {wins('accurate', 'without')} | {wins('accurate', 'with')} |",
        ]
    lines += ["", "## 逐个商品", "", "| 商品 | 可用细节 | 覆盖 不检索/检索 | 无依据数字 不检索/检索 | 盲评（具体/准确） |",
              "| --- | --- | --- | --- | --- |"]
    for row in rows:
        if row not in done:
            reason = row.get("without", {}).get("error") or row.get("with", {}).get("error") or ""
            lines.append(f"| {row['product']} | {row['clauses']} | 生成失败：{reason[:40]} | | |")
            continue
        verdict = row.get("judge", {})
        label = {"with": "检索", "without": "不检索", "tie": "持平"}
        lines.append(
            f"| {row['product']} | {row['clauses']} | {row['without']['covered']} / {row['with']['covered']} | "
            f"{len(row['without']['unsupported_numbers'])} / {len(row['with']['unsupported_numbers'])} | "
            + (f"{label.get(verdict.get('specific'), '-')} / {label.get(verdict.get('accurate'), '-')}" if verdict else "-")
            + " |")
    if done:
        sample = max(done, key=lambda row: row["with"]["covered"] - row["without"]["covered"])
        lines += ["", f"## 示例：{sample['product']}", "", "**不检索**", "", "> " + sample["without"]["text"]
                  .replace("\n", "\n> "), "", "**检索增强**", "", "> " + sample["with"]["text"].replace("\n", "\n> ")]
    docs = ROOT / "docs"
    (docs / "rag-evaluation.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (docs / "rag-evaluation.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(lines[:16]))


if __name__ == "__main__":
    main()
