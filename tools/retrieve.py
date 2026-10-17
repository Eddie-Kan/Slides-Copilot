#!/usr/bin/env python3
"""检索设计段落与关联图片；仅使用 Python 标准库。"""

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "rag/index.json"
LINK = re.compile(r"(!?)\[[^\]]*\]\(([^)]+)\)")

# 将口语反馈对应到设计术语，仍由实际文本匹配决定排序。
ALIASES = [
    (("太空", "不够满", "填充", "排多满", "饱满"), "填充度 主体占比 视觉重量 图文融合"),
    (("太挤", "拥挤", "字多", "内容很多", "信息很多", "密集"), "信息密度 分组 层级 间距 拆页"),
    (("贴上去", "拼贴", "融图", "不自然"), "光影 色温 轮廓光 透视 接触阴影"),
    (("悬浮", "漂浮", "落地"), "接触阴影 投影 地面"),
    (("高级", "质感", "廉价"), "材质 光影 主体 比例 纹理"),
    (("灰蒙", "灰脏", "通透", "发灰"), "明暗 对比 渐变 亮度"),
    (("看不清", "看不见", "可读", "太小"), "可读性 文字 明暗 对比 尺寸"),
    (("错字", "乱码", "漏字", "改字", "文字有错", "只改标题"), "文字 逐字核对 局部编辑 准确文案 成稿"),
    (("样稿", "试做", "几页", "三页"), "样稿 数量 默认 选页"),
    (("不知道怎么问", "沟通", "对接", "需求", "只有主题"), "需求对接 材料诊断 表达目的 关键问题"),
    (("参考", "范例", "喜欢这种", "像这张"), "配色 填充度 视觉气质 机动版式"),
    (("整套", "单调", "都一样", "统一"), "叙事节奏 焦点变化 构图 跨页"),
    (("背景", "素稿", "成稿", "分步生成"), "生图 参考图片 准确文案"),
    (("图表", "饼图", "柱状", "数据"), "数据关系 口径 比较 单位 图表"),
    (("资料不足", "没有素材", "资料不全"), "材料诊断 素材缺口 补资料"),
    (("宽屏", "大屏", "舞台", "发布会"), "屏幕 比例 灯光 彩排 内容禁区"),
]
STOP = set("这个 那个 我们 你们 他们 如何 怎样 什么 一个 以及 进行 可以 需要 应该 当前 实际 是否 其中 这样 那样 的是 的一 一张 一种 不要 不能".split())


def tokens(text):
    result = re.findall(r"[a-z0-9]+", text.lower())
    for phrase in re.findall(r"[\u3400-\u9fff]+", text):
        result.extend(phrase[i:i + 2] for i in range(len(phrase) - 1))
    return [term for term in result if term not in STOP]


def local_links(path, text):
    for image, target in LINK.findall(text):
        if "://" in target or target.startswith("#"):
            continue
        yield bool(image), (path.parent / target.split("#")[0]).resolve()


def chunks(path, text):
    """说明图保持整组；方法文档按二级标题切分。"""
    if path.parent.name == "visuals":
        yield 1, text.splitlines()[0].lstrip("# "), text.strip()
        return
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith("## ")]
    if not starts:
        yield 1, lines[0].lstrip("# "), text.strip()
        return
    for start, end in zip(starts, starts[1:] + [len(lines)]):
        yield start + 1, lines[start][3:], "\n".join(lines[start:end]).strip()


def build_index():
    entries = []
    files = sorted(path for folder in ("handbook", "visuals", "design-library", "prompts")
                   for path in (ROOT / folder).glob("*.md") if path.name != "README.md")
    for path in files:
        text = path.read_text(encoding="utf-8")
        title = text.splitlines()[0].lstrip("# ")
        own_images = {p for is_image, p in local_links(path, text) if is_image}
        for line, section, body in chunks(path, text):
            images = set(own_images)
            notes = set()
            for is_image, linked in local_links(path, body):
                if not is_image and linked.parent == ROOT / "visuals" and linked.name != "README.md":
                    notes.add(linked)
                    linked_text = linked.read_text(encoding="utf-8")
                    images.update(p for is_img, p in local_links(linked, linked_text) if is_img)
            keywords = " ".join(re.findall(r"关键词：([^\n]+)", body))
            search_text = f"{title} {section} {section} {keywords} {keywords} {body}"
            entries.append({
                "id": f"{path.relative_to(ROOT).as_posix()}:{line}",
                "path": path.relative_to(ROOT).as_posix(), "line": line,
                "title": title, "section": section, "text": body,
                "images": sorted(p.relative_to(ROOT).as_posix() for p in images),
                "visual_notes": sorted(p.relative_to(ROOT).as_posix() for p in notes),
                "terms": dict(Counter(tokens(search_text))),
            })
    INDEX.parent.mkdir(exist_ok=True)
    INDEX.write_text(json.dumps({"method": "BM25 + Chinese bigrams + design aliases", "entries": entries},
                                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return entries


def search(entries, query, top):
    weighted = {term: 1.0 for term in tokens(query)}
    for triggers, expansion in ALIASES:
        if any(word in query for word in triggers):
            for term in tokens(expansion):
                weighted.setdefault(term, 0.3)
    counts = Counter(term for entry in entries for term in entry["terms"])
    lengths = [sum(entry["terms"].values()) for entry in entries]
    average = sum(lengths) / len(lengths)
    ranked = []
    for entry, length in zip(entries, lengths):
        score = 0.0
        for term, weight in weighted.items():
            freq = entry["terms"].get(term, 0)
            if freq:
                idf = math.log(1 + (len(entries) - counts[term] + 0.5) / (counts[term] + 0.5))
                score += weight * idf * freq * 2.2 / (freq + 1.2 * (0.4 + 0.6 * length / average))
        if score:
            result = {k: v for k, v in entry.items() if k != "terms"}
            result["score"] = round(score, 3)
            result["source"] = f"{ROOT / entry['path']}:{entry['line']}"
            result["image_paths"] = [str(ROOT / image) for image in entry["images"]]
            ranked.append(result)
    return sorted(ranked, key=lambda item: item["score"], reverse=True)[:top]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="?", help="设计问题或口语反馈")
    parser.add_argument("--top", type=int, default=4, help="返回段落数，默认 4")
    parser.add_argument("--json", action="store_true", help="输出包含文字与图片路径的 JSON")
    parser.add_argument("--rebuild", action="store_true", help="更新知识索引")
    args = parser.parse_args()
    if not args.query and not args.rebuild:
        parser.error("请输入设计问题，或使用 --rebuild 更新索引")
    if args.rebuild or not INDEX.exists():
        entries = build_index()
        print(f"已索引 {len(entries)} 个知识段落。", file=sys.stderr)
    else:
        entries = json.loads(INDEX.read_text(encoding="utf-8"))["entries"]
    if not args.query:
        return
    results = search(entries, args.query, args.top)
    if args.json:
        print(json.dumps({"query": args.query, "results": results}, ensure_ascii=False, indent=2))
        return
    for i, result in enumerate(results, 1):
        print(f"\n[{i}] {result['title']} / {result['section']}\n位置：{result['source']}\n")
        print(result["text"])
        if result["visual_notes"]:
            print("\n关联说明：" + "、".join(result["visual_notes"]))
        for path in result["image_paths"]:
            print(f"打开图片：{path}")
    if not results:
        print("未找到匹配段落，试试具体的设计术语或查看 handbook/README.md。")


if __name__ == "__main__":
    main()
