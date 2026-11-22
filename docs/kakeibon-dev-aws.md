# 開発の kakeibon 用 AWS リソースの作成と、本番テーブルの kakeibon-dev → kakeibon-prod への移行

開発クラスタ（altair）で kakeibon を動かす前に、開発用の AWS リソースを用意する。
本番が `kakeibon-dev` という名前のテーブルを使っているので、本番を `kakeibon-prod` に移し、`kakeibon-dev` はデータを残したまま開発用にする。

- 実行場所の表記: **[Mac]** = aws / kubectl を使う端末。aws は IAM・CloudFormation・DynamoDB を操作できる権限のプロファイルで実行する
- **手順 1 は本番に影響する（最初に行う）**。作業中は kakeibon を操作しない。手順 2・3 は本番に影響しない
- **進み具合（2026-11-22）**: 手順 1〜3 は完了。開発の kakeibon は services/kakeibon/overlays/dev で動いている（[kakeibon.md](kakeibon.md)）。残りは「手順 4: 次の作業」

---

## 今の状態（2026-11-16 調査）

| リソース | 状態 |
|---|---|
| DynamoDB `kakeibon-dev` | **本番が使用中**。kakeibon リポジトリの `cloudformation/DynamoDB.yaml` を `Environment=dev` で作ったもの（スタック `kakeibon-dynamodb`）。54 件 / 120KB、PAY_PER_REQUEST、GSI 2 つ（GSI1・GSI2）、TTL（`ttl`）有効、PITR 有効、Streams なし |
| S3 `kakeibo-data-bucket` | 本番が使用中。手作業で作成（テンプレートなし）。パブリックアクセス全遮断、SSE-S3、オブジェクト所有者の強制（BucketOwnerEnforced）。バージョニング・CORS・ライフサイクルなし |
| S3 `kakeibo-user-bucket` | **使っていない**（ConfigMap の `S3_USER_BUCKET_NAME` に名前があるだけ）。開発用は作らず、新しいロールにも権限を付けない |
| SES | ドメイン ID `kawashima-kazuh.com`（スタック `kakeibon-ses`）。送信元 `noreplykawashima-kazuh.com`。開発も共用する（新しいリソースは不要） |
| Secrets Manager | 本番の JWT は `kakeibon/jwt-secret`。イメージ取得用の PAT は `kappaas-apps/ghcr/github-personal-access-token` |
| 本番の kakeibon-backend の IAM | 共用の aws-credential-helper 経由で、手作業で作った `KppaasRole`（インラインポリシー `KakeibonESOPolicy`）を使う。DynamoDB は `kakeibon-dev` だけ許可 |
| 開発の kakeibon-backend の IAM | なし（開発には共用の aws-credential-helper が無い） |

- DynamoDB はテーブル名を変更できない。新しいテーブル `kakeibon-prod` を作ってデータをコピーする
- `kakeibon-dev` テーブル自体（スタック `kakeibon-dynamodb`）は残し、開発用にする（データは削除しない。1-7）

## 変更後

| | 本番 | 開発 |
|---|---|---|
| DynamoDB | `kakeibon-prod`（新規スタック `kakeibon-dynamodb-prod`） | `kakeibon-dev`（既存スタック `kakeibon-dynamodb`。移行時点の本番データのコピーが残っている） |
| S3 | `kakeibo-data-bucket`（既存のまま） | `kakeibo-data-bucket-dev`（新規スタック `kakeibon-s3-dev`） |
| SES | `kawashima-kazuh.com`（共用） | 同左 |
| JWT | `kakeibon/jwt-secret` | `kakeibon-dev/jwt-secret`（新規） |
| kakeibon-backend の IAM ロール | 当面 `KppaasRole`（段階 2 で `KakeibonBackendRole` に移す） | `KakeibonBackendDevRole`（新規。使うのは段階 2 のサイドカー化の後） |
| ESO（external-secrets）の読み取り範囲 | `kappaas-apps/*`、`kakeibon/*` | `kappaas-apps/repositories/*`、`kappaas-apps/ghcr/*`、`kakeibon-dev/*` |

---

## 手順 1: 本番テーブルの移行（本番に影響あり。2026-11-19 完了）

**作業前の注意**
- 作業中（1-4 のコピー〜1-5 の切り替え）は kakeibon を操作しない。その間に `kakeibon-dev` に書き込まれたデータは `kakeibon-prod` に入らない（入った場合は 1-4 をもう一度実行する）
- 本番の root-app は `main` / `applications/base` のままでよい（kakeibon-apps の main を変更するだけ）
- コピー・削除のスクリプトは `tools/dynamodb/`（kappaas-apps）。aws CLI を使う（boto3 不要）

### 1-1. バックアップ

```bash
# [Mac]
aws dynamodb create-backup --region ap-northeast-1 \
  --table-name kakeibon-dev --backup-name kakeibon-dev-before-rename-$(date +%Y%m%d)
aws dynamodb list-backups --region ap-northeast-1 --table-name kakeibon-dev \
  --query 'BackupSummaries[].[BackupName,BackupStatus]' --output text      # AVAILABLE
```

- [ ] バックアップ作成（AVAILABLE）

### 1-2. kakeibon-prod テーブルを作る

既存の `DynamoDB.yaml` をそのまま使う（`Environment=prod` → テーブル名 `kakeibon-prod`。GSI・TTL・PITR もテンプレートどおり）。

```bash
# [Mac] kakeibon リポジトリのルートで
aws cloudformation deploy --region ap-northeast-1 \
  --stack-name kakeibon-dynamodb-prod \
  --template-file cloudformation/DynamoDB.yaml \
  --parameter-overrides Environment=prod

aws dynamodb describe-table --region ap-northeast-1 --table-name kakeibon-prod \
  --query 'Table.[TableStatus,ItemCount,length(GlobalSecondaryIndexes)]' --output text   # ACTIVE 0 2
aws dynamodb describe-time-to-live --region ap-northeast-1 --table-name kakeibon-prod --output text   # ttl ENABLED
```

- [ ] `kakeibon-prod` 作成（ACTIVE、GSI 2、TTL 有効）

### 1-3. KppaasRole に kakeibon-prod への権限を追加

本番の kakeibon-backend は今 KppaasRole を使っている。テーブルを切り替える前に、`KakeibonESOPolicy` の DynamoDB の文に `kakeibon-prod` を足す（`kakeibon-dev` は 1-7 で外す）。

```bash
# [Mac] 作業用ディレクトリ
mkdir -p ~/k8s-backup/kakeibon-rename && cd ~/k8s-backup/kakeibon-rename
aws iam get-role-policy --role-name KppaasRole --policy-name KakeibonESOPolicy \
  --query PolicyDocument --output json > policy-before.json

# DynamoDB の文（Sid: DynamoDB）の Resource に kakeibon-prod とその index を追加
python3 - <<'EOF'
import json
d = json.load(open("policy-before.json"))
st = next(s for s in d["Statement"] if s.get("Sid") == "DynamoDB")
for r in ["arn:aws:dynamodb:ap-northeast-1:391726422976:table/kakeibon-prod",
          "arn:aws:dynamodb:ap-northeast-1:391726422976:table/kakeibon-prod/*"]:
    if r not in st["Resource"]:
        st["Resource"].append(r)
json.dump(d, open("policy-after.json", "w"), indent=2)
print(st["Resource"])
EOF

diff <(python3 -m json.tool policy-before.json) <(python3 -m json.tool policy-after.json)   # 2 行の追加だけ
aws iam put-role-policy --role-name KppaasRole --policy-name KakeibonESOPolicy \
  --policy-document file://policy-after.json

# 確認（allowed になること）
aws iam simulate-principal-policy \
  --policy-source-arn arn:aws:iam::391726422976:role/KppaasRole \
  --action-names dynamodb:PutItem dynamodb:Query \
  --resource-arns arn:aws:dynamodb:ap-northeast-1:391726422976:table/kakeibon-prod \
  --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output text
```

- [ ] KppaasRole が kakeibon-prod に PutItem / Query できる（allowed）

### 1-4. データをコピー

```bash
# [Mac] kappaas-apps のルートで
python3 tools/dynamodb/copy-table.py kakeibon-dev kakeibon-prod
#   kakeibon-dev: 54 件
#   kakeibon-prod: 54 件（コピー後）     ← 件数が一致すること
```

- 同じキーのアイテムは上書きするので、何度実行してもよい
- コピー元から消したアイテムは、コピー先からは消えない（作業中に kakeibon を操作しなければ問題ない）

- [ ] 件数一致

### 1-5. 本番の kakeibon-backend を kakeibon-prod に切り替える

kakeibon-apps の main（本番が同期するブランチ）で `components/backend/configmap.yaml` を変更し、PR → マージ。

```yaml
  DYNAMODB_TABLE_NAME: "kakeibon-prod"     # kakeibon-dev から変更
```

Argo CD が同期したら、Pod を再起動する（ConfigMap の値は環境変数として起動時に読まれるので、変更だけでは反映されない）。

```bash
# [Mac]
kubectl --context prod -n kakeibon get cm kakeibon-backend-config -o jsonpath='{.data.DYNAMODB_TABLE_NAME}{"\n"}'   # kakeibon-prod
kubectl --context prod -n kakeibon rollout restart deploy/kakeibon-backend
kubectl --context prod -n kakeibon rollout status deploy/kakeibon-backend
kubectl --context prod -n kakeibon exec deploy/kakeibon-backend -c backend -- printenv DYNAMODB_TABLE_NAME          # kakeibon-prod
```

- [ ] backend の環境変数が kakeibon-prod

### 1-6. 動作確認

ブラウザで kakeibon を操作する:

- [ ] ログインできる
- [ ] 既存のデータが表示される
- [ ] 登録・削除ができ、`kakeibon-prod` の件数が変わる（`aws dynamodb scan --table-name kakeibon-prod --select COUNT --region ap-northeast-1`）。`kakeibon-dev` の件数は変わらない
- [ ] backend のログにエラーが無い

  ```bash
  kubectl --context prod -n kakeibon logs deploy/kakeibon-backend -c backend --since=30m | grep -iE 'denied|ResourceNotFound|error'
  ```

### 1-7. kakeibon-dev の扱い（データは残す）

2026-11-19 の判断で、kakeibon-dev のデータは削除しない。移行時点の本番データのコピーが入ったまま、開発用のテーブルとして使う。

- 開発クラスタの kakeibon からは、移行時点の本番データ（個人のデータ）が見える。開発で書き込んだ内容は本番には反映されない
- データを消したくなったら、1-6 の確認が済んでいることを確かめてから実行する（本番データは 1-1 のバックアップと `kakeibon-prod` にある）

  ```bash
  # [Mac] kappaas-apps のルートで（確認のためテーブル名をもう一度入力する）
  python3 tools/dynamodb/empty-table.py kakeibon-dev
  ```

データとは別に、KppaasRole から `kakeibon-dev` への権限は外す（本番の kakeibon-backend が開発用のテーブルに触れないように）:

```bash
# [Mac] ~/k8s-backup/kakeibon-rename で
python3 - <<'EOF'
import json
d = json.load(open("policy-after.json"))
st = next(s for s in d["Statement"] if s.get("Sid") == "DynamoDB")
st["Resource"] = [r for r in st["Resource"] if "/kakeibon-dev" not in r]
json.dump(d, open("policy-final.json", "w"), indent=2)
print(st["Resource"])          # kakeibon-prod の 2 つだけ
EOF
aws iam put-role-policy --role-name KppaasRole --policy-name KakeibonESOPolicy \
  --policy-document file://policy-final.json
```

- [ ] KppaasRole の DynamoDB の Resource が kakeibon-prod だけ
- [ ] 1-6 の確認がもう一度 OK

---

## 手順 2: テンプレートの追加・変更（PR。2026-11-22 完了: kakeibon #77、kappaas-apps #10）

### 2-1. kakeibon リポジトリ: `cloudformation/S3.yaml`（新規）

`DynamoDB.yaml` と同じ形。バケット名は `kakeibo-data-bucket-<環境>`（使っていない `kakeibo-user-bucket` に相当するものは作らない）。設定は本番の既存バケットと同じ。
本番の既存バケット（環境名なし）はこのテンプレートでは作らない・触らない。

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: CloudFormation template for creating the kakeibon S3 bucket

Parameters:
  ProjectName:
    Type: String
    Default: kakeibon
    Description: Project name used for tagging

  Environment:
    Type: String
    Default: dev
    AllowedValues:
      - dev
      - stg
      - prod
    Description: Deployment environment

Resources:
  # 家計簿データ（S3_DATA_BUCKET_NAME）
  DataBucket:
    Type: AWS::S3::Bucket
    DeletionPolicy: Retain
    UpdateReplacePolicy: Retain
    Properties:
      BucketName: !Sub kakeibo-data-bucket-${Environment}
      PublicAccessBlockConfiguration:
        BlockPublicAcls: true
        BlockPublicPolicy: true
        IgnorePublicAcls: true
        RestrictPublicBuckets: true
      BucketEncryption:
        ServerSideEncryptionConfiguration:
          - ServerSideEncryptionByDefault:
              SSEAlgorithm: AES256
      OwnershipControls:
        Rules:
          - ObjectOwnership: BucketOwnerEnforced
      Tags:
        - Key: Project
          Value: !Ref ProjectName
        - Key: Environment
          Value: !Ref Environment

Outputs:
  DataBucketName:
    Value: !Ref DataBucket
  DataBucketArn:
    Value: !GetAtt DataBucket.Arn
```

```bash
# [Mac] kakeibon リポジトリのルートで
aws cloudformation validate-template --template-body file://cloudformation/S3.yaml
```

- [x] S3.yaml を追加して PR（kakeibon の main へ。#77）

### 2-2. kappaas-apps: `aws/rolesanywhere.yaml` に KakeibonBackendRole を追加

feature ブランチを作り、次の 4 か所を変更して dev への PR を作る。

**(1) 冒頭のコメント**

```yaml
# ロールは利用者ごとに分ける（docs/iam-roles-anywhere.md）。external-secrets コントローラー用と kakeibon-backend 用。
```

（「今は external-secrets コントローラー用だけ。」と「kakeibon-backend 用（KakeibonBackendRole）は…追加する。」の 2 行を置き換える）

**(2) Mappings**: dev に ESO の読み取り範囲と kakeibon-backend 用の値を、prod に kakeibon-backend 用の値を追加

```yaml
Mappings:
  EnvironmentMap:
    dev:
      Name: kappaas-dev
      ExternalSecretsRoleName: KappaasDevExternalSecretsRole
      # Argo CD のリポジトリ認証情報（argocd-config/repositories）、イメージ取得用の PAT、開発の kakeibon のシークレット
      SecretArns:
        - arn:aws:secretsmanager:ap-northeast-1:391726422976:secret:kappaas-apps/repositories/*
        - arn:aws:secretsmanager:ap-northeast-1:391726422976:secret:kappaas-apps/ghcr/*      # 追加
        - arn:aws:secretsmanager:ap-northeast-1:391726422976:secret:kakeibon-dev/*           # 追加
      KakeibonBackendRoleName: KakeibonBackendDevRole                                         # 以下追加
      KakeibonTableArn: arn:aws:dynamodb:ap-northeast-1:391726422976:table/kakeibon-dev
      KakeibonBucketArns:
        - arn:aws:s3:::kakeibo-data-bucket-dev
      KakeibonObjectArns:
        - arn:aws:s3:::kakeibo-data-bucket-dev/*
    prod:
      Name: kappaas-prod
      ExternalSecretsRoleName: KappaasExternalSecretsRole
      # 現在の本番ロール（KppaasRole）の external-secrets 用の権限と同じ範囲
      SecretArns:
        - arn:aws:secretsmanager:ap-northeast-1:391726422976:secret:kappaas-apps/*
        - arn:aws:secretsmanager:ap-northeast-1:391726422976:secret:kakeibon/*
      KakeibonBackendRoleName: KakeibonBackendRole                                            # 以下追加
      KakeibonTableArn: arn:aws:dynamodb:ap-northeast-1:391726422976:table/kakeibon-prod
      KakeibonBucketArns:
        - arn:aws:s3:::kakeibo-data-bucket
      KakeibonObjectArns:
        - arn:aws:s3:::kakeibo-data-bucket/*
```

**(3) Resources**: `Profile:` の前に KakeibonBackendRole を追加し、Profile の RoleArns に加える

権限は [iam-roles-anywhere.md](iam-roles-anywhere.md) の「KakeibonBackendRole の権限ポリシー」と同じ。ただし S3 は使っていない `kakeibo-user-bucket` を外し、バケットへの操作とオブジェクトへの操作を分けた。

```yaml
  # kakeibon-backend のサイドカー（aws_signing_helper）が引き受けるロール
  KakeibonBackendRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: !FindInMap [EnvironmentMap, !Ref Environment, KakeibonBackendRoleName]
      Description: !Sub Assumed by the kakeibon-backend sidecar on the ${Environment} cluster via IAM Roles Anywhere
      MaxSessionDuration: 3600
      Tags:
        - Key: Environment
          Value: !Ref Environment
        - Key: Project
          Value: kappaas
      AssumeRolePolicyDocument:
        Version: "2012-10-17"
        Statement:
          - Effect: Allow
            Principal:
              Service: rolesanywhere.amazonaws.com
            Action:
              - sts:AssumeRole
              - sts:TagSession
              - sts:SetSourceIdentity
            Condition:
              # この環境のトラストアンカーで検証された、CN=kakeibon-backend の証明書だけ
              # （CN は kakeibon-backend の Certificate の commonName と合わせる。iam-roles-anywhere.md の段階 2）
              ArnEquals:
                aws:SourceArn: !GetAtt TrustAnchor.TrustAnchorArn
              StringEquals:
                aws:PrincipalTag/x509Subject/CN: kakeibon-backend
      Policies:
        - PolicyName: KakeibonBackend
          PolicyDocument:
            Version: "2012-10-17"
            Statement:
              - Sid: DynamoDB
                Effect: Allow
                Action:
                  - dynamodb:GetItem
                  - dynamodb:PutItem
                  - dynamodb:UpdateItem
                  - dynamodb:DeleteItem
                  - dynamodb:Scan
                  - dynamodb:Query
                  - dynamodb:BatchWriteItem
                  - dynamodb:DescribeTable
                  - dynamodb:BatchGetItem
                Resource:
                  - !FindInMap [EnvironmentMap, !Ref Environment, KakeibonTableArn]
                  - !Sub
                    - ${TableArn}/*
                    - TableArn: !FindInMap [EnvironmentMap, !Ref Environment, KakeibonTableArn]
              - Sid: S3Bucket
                Effect: Allow
                Action:
                  - s3:ListBucket
                Resource: !FindInMap [EnvironmentMap, !Ref Environment, KakeibonBucketArns]
              - Sid: S3Object
                Effect: Allow
                Action:
                  - s3:GetObject
                  - s3:PutObject
                  - s3:DeleteObject
                Resource: !FindInMap [EnvironmentMap, !Ref Environment, KakeibonObjectArns]
              - Sid: SES
                Effect: Allow
                Action:
                  - ses:SendEmail
                  - ses:SendRawEmail
                Resource: arn:aws:ses:ap-northeast-1:391726422976:identity/kawashima-kazuh.com
                Condition:
                  StringEquals:
                    ses:FromAddress: noreplykawashima-kazuh.com

  Profile:
    Type: AWS::RolesAnywhere::Profile
    Properties:
      Name: !FindInMap [EnvironmentMap, !Ref Environment, Name]
      Enabled: true
      RoleArns:
        - !GetAtt ExternalSecretsRole.Arn
        - !GetAtt KakeibonBackendRole.Arn        # 追加
```

**(4) Outputs** に追加

```yaml
  # kakeibon-backend のサイドカーに渡す（iam-roles-anywhere.md の段階 2）
  KakeibonBackendRoleArn:
    Value: !GetAtt KakeibonBackendRole.Arn
```

```bash
# [Mac] kappaas-apps のルートで
aws cloudformation validate-template --template-body file://aws/rolesanywhere.yaml
```

- 2026-11-16 に、この変更を当てたテンプレートで validate-template が通ることを確認済み
- prod の値（kakeibon-prod など）は、本番スタックを作るとき（段階 1 の本番分）にそのまま使われる。手順 1 で kakeibon-prod を作ってから本番スタックを作ること

- [x] rolesanywhere.yaml を変更して dev への PR → マージ（#10）

---

## 手順 3: 開発用リソースの作成（本番に影響なし。2026-11-22 完了）

### 3-1. S3 バケット

```bash
# [Mac] kakeibon リポジトリのルートで（2-1 の S3.yaml）
aws cloudformation deploy --region ap-northeast-1 \
  --stack-name kakeibon-s3-dev \
  --template-file cloudformation/S3.yaml \
  --parameter-overrides Environment=dev

aws cloudformation describe-stacks --region ap-northeast-1 --stack-name kakeibon-s3-dev \
  --query 'Stacks[0].Outputs' --output table
aws s3api get-public-access-block --bucket kakeibo-data-bucket-dev     # 4 つとも true
```

- バケット名は全世界で一意。作成に失敗した（他のアカウントが使用中）場合は、S3.yaml の名前を変えて、以降の手順の名前も読み替える

- [x] `kakeibo-data-bucket-dev` 作成

### 3-2. 開発用の JWT シークレット

値はランダムに生成し、画面に出さない。本番の `kakeibon/jwt-secret` と同じく文字列だけを入れる（ExternalSecret は値全体を `JWT_SECRET` にする）。

```bash
# [Mac]
aws secretsmanager create-secret --region ap-northeast-1 \
  --name kakeibon-dev/jwt-secret \
  --description "JWT secret for kakeibon on the dev cluster" \
  --tags Key=Environment,Value=dev Key=Project,Value=kakeibon \
  --secret-string "$(openssl rand -base64 48)"
```

- [x] `kakeibon-dev/jwt-secret` 作成

### 3-3. 開発の rolesanywhere スタックを更新

2-2 の PR をマージしてから。開発の CA 証明書（`~/k8s-backup/dev-ca.crt`）は [dev-cluster.md](dev-cluster.md) の手順 7 で取り出したもの。

```bash
# [Mac] kappaas-apps のルートで（dev ブランチ）
aws cloudformation deploy --region ap-northeast-1 \
  --stack-name kappaas-dev-rolesanywhere \
  --template-file aws/rolesanywhere.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides Environment=dev CaCertificate="$(cat ~/k8s-backup/dev-ca.crt)"

aws cloudformation describe-stacks --region ap-northeast-1 --stack-name kappaas-dev-rolesanywhere \
  --query 'Stacks[0].Outputs' --output table      # KakeibonBackendRoleArn が増えている
```

確認（ESO の読み取り範囲が増えたこと。開発クラスタのサイドカーは次に資格情報を更新したときから新しい権限を使う）:

```bash
aws iam get-role-policy --role-name KappaasDevExternalSecretsRole --policy-name ReadSecrets \
  --query 'PolicyDocument.Statement[0].Resource' --output text      # ghcr と kakeibon-dev/* がある
aws iam get-role --role-name KakeibonBackendDevRole --query 'Role.Tags' --output text
```

- [x] 開発の rolesanywhere スタック更新（KakeibonBackendDevRole 作成）

---

## 戻し方

| 止めたところ | 戻し方 |
|---|---|
| 1-2〜1-4（切り替え前） | 本番は kakeibon-dev のままなので影響なし。やり直す場合は `kakeibon-prod` を空にして（`empty-table.py kakeibon-prod`）1-4 から |
| 1-5・1-6（切り替え後に問題） | kakeibon-apps の ConfigMap を `kakeibon-dev` に戻す PR → マージ → `rollout restart`。切り替え後に kakeibon-prod に書いたデータは `copy-table.py kakeibon-prod kakeibon-dev` で戻す |

KppaasRole のポリシーは `policy-before.json` を put-role-policy すれば元に戻る。

移行前のデータを確認したいときは、1-1 のバックアップを別名のテーブルに復元する（`aws dynamodb restore-table-from-backup --target-table-name <別名> --backup-arn ...`。既存のテーブルには復元できない）。

---

## 手順 4: 次の作業

- [x] 開発の kakeibon（2026-11-22 完了）
  - マニフェストは kakeibon-apps から `services/kakeibon` に移した（#9）。開発用の値（テーブル `kakeibon-dev`、バケット `kakeibo-data-bucket-dev`、JWT `kakeibon-dev/jwt-secret`、ホスト名 `kakeibon-dev.local`）は `overlays/dev` で切り替える
  - 開発の kakeibon-backend のサイドカー化（[iam-roles-anywhere.md](iam-roles-anywhere.md) の段階 2、ロール `KakeibonBackendDevRole`）も実施（#12）。backend・client とも Ready、http://kakeibon-dev.local で開ける
- [ ] 本番の rolesanywhere スタック（段階 1 の本番分）を作り、external-secrets と kakeibon-backend のサイドカーを本番にも入れる（KakeibonBackendRole は kakeibon-prod を指す）
- 手順 1 の文中の「kakeibon-apps の main の `components/backend/configmap.yaml`」は、本番の release 切り替え後は `services/kakeibon/overlays/prod/patches/backend/configmap-patch.yaml`
