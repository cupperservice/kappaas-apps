#!/usr/bin/env python3
"""DynamoDB テーブルの全アイテムを削除する（テーブル自体は残す。aws CLI を使う）。
使い方: python3 empty-table.py <テーブル名>   ※確認のためテーブル名をもう一度入力する
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
    if len(sys.argv) != 2:
        sys.exit("使い方: empty-table.py <テーブル名>")
    table = sys.argv[1]
    keys = [k["AttributeName"] for k in aws("dynamodb", "describe-table", "--table-name", table)["Table"]["KeySchema"]]
    items = aws("dynamodb", "scan", "--table-name", table,
                "--projection-expression", ", ".join(f"#k{i}" for i in range(len(keys))),
                "--expression-attribute-names", json.dumps({f"#k{i}": k for i, k in enumerate(keys)}))["Items"]
    if input(f"{table} の {len(items)} 件を削除します。テーブル名を入力: ") != table:
        sys.exit("中止しました")
    for i in range(0, len(items), 25):
        pending = {table: [{"DeleteRequest": {"Key": it}} for it in items[i:i + 25]]}
        for _ in range(10):
            with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
                json.dump(pending, f)
                f.flush()
                pending = aws("dynamodb", "batch-write-item",
                              "--request-items", f"file://{f.name}").get("UnprocessedItems", {})
            if not pending:
                break
        else:
            sys.exit(f"{i} 件目付近の削除が終わりません")
    count = aws("dynamodb", "scan", "--table-name", table, "--select", "COUNT")["Count"]
    print(f"{table}: {count} 件（削除後）")


if __name__ == "__main__":
    main()
