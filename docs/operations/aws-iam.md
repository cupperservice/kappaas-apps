# AWS の認証（IAM Roles Anywhere）

クラスタの中から AWS を使うものは、IAM Roles Anywhere で IAM ロールを引き受ける。アクセスキーは使わない。
設計の経緯は [history/2026-11-iam-roles-anywhere.md](../history/2026-11-iam-roles-anywhere.md)。

## 仕組み

```
cert-manager の CA（ClusterIssuer k8s-ca、CN k8s-on-prem-ca）＝ IAM Roles Anywhere のトラストアンカー
   │ 証明書を発行
   ├─ external-secrets コントローラーの Pod
   │    ├─ aws-credential-helper（ネイティブサイドカー、aws_signing_helper serve、127.0.0.1:9911）
   │    │    証明書 CN=external-secrets → KappaasExternalSecretsRole（開発 KappaasDevExternalSecretsRole）
   │    └─ external-secrets … AWS_EC2_METADATA_SERVICE_ENDPOINT=http://127.0.0.1:9911
   └─ kakeibon-backend の Pod
        ├─ aws-credential-helper（同上）
        │    証明書 CN=kakeibon-backend → KakeibonBackendRole（開発 KakeibonBackendDevRole）
        └─ backend … AWS_EC2_METADATA_SERVICE_ENDPOINT=http://127.0.0.1:9911
```

| 利用者 | 証明書（CN） | ロール（本番 / 開発） | 権限 |
|---|---|---|---|
| external-secrets コントローラー | `external-secrets` | `KappaasExternalSecretsRole` / `KappaasDevExternalSecretsRole` | Secrets Manager の読み取り（`aws/rolesanywhere.yaml` の `SecretArns`） |
| kakeibon-backend | `kakeibon-backend` | `KakeibonBackendRole` / `KakeibonBackendDevRole` | その環境の DynamoDB テーブル・S3 バケット、SES の送信 |

- トラストアンカー・ロール・プロファイルは CloudFormation のスタック（[aws/rolesanywhere.yaml](../../aws/rolesanywhere.yaml)、環境ごとに `kappaas-<env>-rolesanywhere`）で作る。作り方は [setup/06-gitops.md](../setup/06-gitops.md) の 4
- ロールの信頼ポリシーは「その環境のトラストアンカー（`aws:SourceArn`）」かつ「証明書の CN」が条件。同じ CA の別の証明書では引き受けられない
- サイドカーは Pod の中の `127.0.0.1:9911` で待ち受けるだけなので、資格情報は Pod の外に出ない。ネイティブサイドカー（`initContainers` に `restartPolicy: Always`）なので、サイドカーの startupProbe が通るまで本体が起動しない
- ARN は環境ごとに `charts/external-secrets/<env>/values.yaml`（`aws.*`）と `services/kakeibon/overlays/<env>/patches/backend/aws-config-patch.yaml`
- 証明書は cert-manager が自動で更新し、サイドカーは資格情報を更新するたびに証明書を読み直す

### 確認

```bash
# [Mac] サイドカーが引き受けたロール名
kubectl --context <env> -n external-secrets exec deploy/external-secrets -c aws-credential-helper -- sh -c '
  T=$(curl -s -X PUT http://127.0.0.1:9911/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
  curl -s -H "X-aws-ec2-metadata-token: $T" http://127.0.0.1:9911/latest/meta-data/iam/security-credentials/; echo'
kubectl --context <env> get secretstore,externalsecret -A          # Ready / SecretSynced
```

## シークレットを追加する

1. Secrets Manager に登録する（[setup/03-external.md](../setup/03-external.md)「Secrets Manager」）
2. そのシークレットが、その環境の ExternalSecretsRole で読める範囲か確認する（`aws/rolesanywhere.yaml` の `Mappings` → `SecretArns`）
   - 範囲外なら `SecretArns` に足して PR → マージ後にスタックを更新する（下）
3. SecretStore（その Namespace のもの）と ExternalSecret をマニフェストに足す

## スタックを更新する

テンプレートを変えたとき、またはクラスタを作り直して CA が変わったとき。

```bash
# [Mac] リポジトリのルートで（テンプレートの変更はマージ済みのブランチで）
kubectl --context <env> get secret k8s-ca-secret -n cert-manager -o jsonpath='{.data.tls\.crt}' | base64 -d > ~/k8s-backup/<env>-ca.crt
aws cloudformation deploy --region ap-northeast-1 --stack-name kappaas-<env>-rolesanywhere \
  --template-file aws/rolesanywhere.yaml --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides Environment=<env> CaCertificate="$(cat ~/k8s-backup/<env>-ca.crt)"
```

- `SecretArnPatterns` を指定すると `SecretArns` の既定は使われない（必要なものをすべてカンマ区切りで書く）。普段はテンプレートの `Mappings` を変える
- 本番のスタックの変更は、本番の external-secrets と kakeibon に影響する。差分を `aws cloudformation deploy --no-execute-changeset` と `describe-change-set` で確認してから実行する

## AWS を使うものを新しく足す

kakeibon-backend と同じ形にする。

1. `aws/rolesanywhere.yaml` にロールを足す（信頼ポリシーの CN を新しい名前に、権限は必要なものだけ）。プロファイルの `RoleArns` にも足す
2. その Pod に、k8s-ca で発行する Certificate（CN を 1 と同じに）・ARN の ConfigMap・サイドカーを足す（`services/kakeibon/base/backend/` の `aws-certificate.yaml`・`aws-config.yaml`・`deployment.yaml` が見本）
3. スタックを更新して、Outputs の ARN を ConfigMap に書く

## 残っている作業: 旧ロール KppaasRole の削除（段階 C-4、2026-12-01 ごろ）

2026-11-23 まで本番で使っていた共用の aws-credential-helper のロール `KppaasRole` と、旧トラストアンカー・旧プロファイルを削除する（経緯は [history/2026-11-prod-migration.md](../history/2026-11-prod-migration.md)）。
今のスタック（`kappaas-prod-rolesanywhere`）のものとは別なので、ID を取り違えない。

```bash
# [Mac] 1. CloudTrail で KppaasRole が 2026-11-23 以降使われていないこと（何も出なければ使われていない）
aws cloudtrail lookup-events --region ap-northeast-1 \
  --lookup-attributes AttributeKey=ResourceName,AttributeValue=arn:aws:iam::391726422976:role/KppaasRole \
  --start-time 2026-11-24 --query 'Events[].[EventTime,EventName]' --output text

# 2. 旧プロファイル・旧トラストアンカーの ID を確認（今のスタックのもの a0d8433e-… / 76d3d14c-…・f02a3107-… / 5b8d5ceb-… 以外）
aws rolesanywhere list-profiles --region ap-northeast-1 --query 'profiles[].[name,profileId,roleArns]' --output json
aws rolesanywhere list-trust-anchors --region ap-northeast-1 --query 'trustAnchors[].[name,trustAnchorId]' --output text

# 3. 削除（旧プロファイルは 591bccad-ba10-4397-b798-5de2d5795f17 のはず。2 で確認した ID を使う）
aws rolesanywhere delete-profile --region ap-northeast-1 --profile-id <旧プロファイルの ID>
aws rolesanywhere delete-trust-anchor --region ap-northeast-1 --trust-anchor-id <旧トラストアンカーの ID>
aws iam list-role-policies --role-name KppaasRole
aws iam list-attached-role-policies --role-name KppaasRole
aws iam delete-role-policy --role-name KppaasRole --policy-name <上で表示された名前>     # インラインポリシーごとに
aws iam detach-role-policy --role-name KppaasRole --policy-arn <アタッチされたポリシー>   # あれば
aws iam delete-role --role-name KppaasRole
```

- [ ] CloudTrail で KppaasRole が使われていないことを確認
- [ ] 旧プロファイル・旧トラストアンカーの削除
- [ ] KppaasRole の削除
- 終わったら、この節を消して history に記録を残す
