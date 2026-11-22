#!/usr/bin/env python3
"""DynamoDB テーブルの全アイテムを別のテーブルにコピーする（aws CLI を使う。boto3 不要）。
使い方: python3 copy-table.py <コピー元> <コピー先>
- コピー先に同じキーのアイテムがあれば上書きする（何度実行してもよい）
- コピー元にないアイテムをコピー先から消すことはしない
"""
import json
import subprocess
import sys
import tempfile

REGION = "ap-northeast-1"


def aws(*args):
    out = subprocess.run(["aws", "--region", REGION, *args, "--output", "json"],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out) if out.strip() else {}


def main():
    if len(sys.argv) != 3:
        sys.exit("使い方: copy-table.py <コピー元> <コピー先>")
    src, dst = sys.argv[1], sys.argv[2]
    # aws CLI v2 の scan はページングを自動で処理して全件返す
    items = aws("dynamodb", "scan", "--table-name", src)["Items"]
    print(f"{src}: {len(items)} 件")
    for i in range(0, len(items), 25):
        pending = {dst: [{"PutRequest": {"Item": it}} for it in items[i:i + 25]]}
        for _ in range(10):  # 処理されなかった分（UnprocessedItems）を再送する
            with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
                json.dump(pending, f)
                f.flush()
                pending = aws("dynamodb", "batch-write-item",
                              "--request-items", f"file://{f.name}").get("UnprocessedItems", {})
            if not pending:
                break
        else:
            sys.exit(f"{i} 件目付近の書き込みが終わりません")
    count = aws("dynamodb", "scan", "--table-name", dst, "--select", "COUNT")["Count"]
    print(f"{dst}: {count} 件（コピー後）")


if __name__ == "__main__":
    main()
