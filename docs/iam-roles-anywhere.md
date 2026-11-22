# IAM Roles Anywhere のロール分割とサイドカー化（external-secrets / kakeibon-backend）

## 環境分割後の実施方針（2026-11-16 追記）

この文書は本番 / 開発に分ける前（2026-11-14）に書いた。今は次の方針で進める。下の設計・ポリシーの考え方はそのまま有効。

| 項目 | 方針 |
|---|---|
| IAM の作り方 | CloudFormation（[aws/rolesanywhere.yaml](../aws/rolesanywhere.yaml)）で環境ごとにスタックを作る（`kappaas-dev-rolesanywhere` / `kappaas-prod-rolesanywhere`）。本番も新しいトラストアンカー・プロファイルを作る（下の「AWS 側の手順」の CLI は使わない） |
| ロール名 | external-secrets: 本番 `KappaasExternalSecretsRole`、開発 `KappaasDevExternalSecretsRole`（kakeibon-backend 用は段階 2 でテンプレートに追加） |
| サイドカーの ARN | 環境ごとに違うので `charts/external-secrets/{prod,dev}/values.yaml` の `aws.*` に書き、ConfigMap `external-secrets-aws` 経由でサイドカーに渡す（サイドカーの定義は共通の `values.yaml` に 1 つ） |
| 進め方 | **段階 1**: external-secrets（このリポジトリ。実装済み）。**段階 2**: kakeibon-backend（**開発は実施済み**: `services/kakeibon/overlays/dev` の `aws-certificate.yaml`・`aws-config.yaml`・`patches/backend/deployment-patch.yaml`。本番は本番の rolesanywhere スタック作成後に base へ移す）。マニフェストは kakeibon-apps から `services/kakeibon` に移した（[kakeibon.md](kakeibon.md)）。下の文中の kakeibon-apps の `components/backend/...` は `services/kakeibon/base/backend/...` と読み替える |
| 共用の aws-credential-helper | 開発: 段階 1 で廃止（`applications/overlays/dev/patches/aws-credential-helper-patch.yaml` で除外）。本番: kakeibon-backend が使うので段階 2 まで残す（KppaasRole のまま） |
| 開発の kakeibon-backend | サイドカーで `KakeibonBackendDevRole` を使う（テーブル `kakeibon-dev`、バケット `kakeibo-data-bucket-dev`、SES は共用。docs/kakeibon-dev-aws.md） |
| 本番テーブル | 本番は 2026-11-19 に `kakeibon-dev` から `kakeibon-prod` に移した（[kakeibon-dev-aws.md](kakeibon-dev-aws.md) の手順 1）。下の文中の本番の `kakeibon-dev` は `kakeibon-prod` と読み替える（aws/rolesanywhere.yaml の KakeibonBackendRole は kakeibon-prod を指す） |

### 段階 1 の手順（external-secrets）

1. **[AWS]** 開発スタックを更新（Outputs の ExternalSecretsRoleArn が `KappaasDevExternalSecretsRole`）。手順は [dev-cluster.md](dev-cluster.md) の 7
2. dev ブランチに push → 開発クラスタで下の「2. external-secrets にサイドカーを追加」の確認（`--context dev`）
3. **[AWS]** 本番スタックを作成（`Environment=prod`、本番の CA）→ Outputs の ARN を `charts/external-secrets/prod/values.yaml` に書く
   ```bash
   kubectl --context prod get secret k8s-ca-secret -n cert-manager -o jsonpath='{.data.tls\.crt}' | base64 -d > ~/k8s-backup/prod-ca.crt
   aws cloudformation deploy --region ap-northeast-1 --stack-name kappaas-prod-rolesanywhere \
     --template-file aws/rolesanywhere.yaml --capabilities CAPABILITY_NAMED_IAM \
     --parameter-overrides Environment=prod CaCertificate="$(cat ~/k8s-backup/prod-ca.crt)"
   ```
4. dev → main → release → 本番で同じ確認（`--context prod`）。本番の旧 helper（DaemonSet）は残るので、問題があれば revert で戻せる

- [ ] 開発スタック更新
- [ ] 開発で確認
- [ ] 本番スタック作成・prod/values.yaml に ARN
- [ ] 本番で確認

## 現状（2026-11-14）

```
cert-manager  ClusterIssuer k8s-ca（CA: k8s-on-prem-ca）
                └─ Certificate aws-credential-helper（CN=k8s-workload、30日・27日目に更新）
                      ↓
aws-credential-helper（DaemonSet、hostNetwork + hostPort 9911、全ノード）
  aws_signing_helper serve --role-arn KppaasRole（127.0.0.1:19911）→ socat で 9911 に公開
                      ↓ Service aws-credential-helper:9911（IMDSv2 互換）
      ┌───────────────┴────────────────┐
external-secrets コントローラー        kakeibon-backend
（Secrets Manager の読み取り）         （DynamoDB / S3 / SES）
```

| 項目             | 値                                                                                                    |
| ---------------- | ----------------------------------------------------------------------------------------------------- |
| トラストアンカー | `arn:aws:rolesanywhere:ap-northeast-1:391726422976:trust-anchor/04787480-a940-4438-8b81-8551d90d2765` |
| プロファイル     | `arn:aws:rolesanywhere:ap-northeast-1:391726422976:profile/591bccad-ba10-4397-b798-5de2d5795f17`      |
| ロール           | `arn:aws:iam::391726422976:role/KppaasRole`（1 つを 2 つの利用者で共用）                              |
| 接続先の設定場所 | external-secrets: `charts/external-secrets/values.yaml` の `extraEnv`<br>kakeibon-backend: `kakeibon-apps/components/backend/deployment.yaml` の `AWS_EC2_METADATA_SERVICE_ENDPOINT` |

### 問題点

1. **権限が混ざっている**: kakeibon-backend から全シークレット（`kappaas-apps/*`、`kakeibon/*`）が読める。external-secrets からも DynamoDB / S3 / SES を操作できる
2. **LAN に公開されている**: hostNetwork + hostPort 9911 のため、同じネットワークの機器が各ノードの `:9911` から KppaasRole の一時資格情報を取得できる（2026-11-14 に Mac から `PUT /latest/api/token` が 200 になることを確認）。クラスタ内も NetworkPolicy が無く、どの Pod からも取得できる
3. **同じ CA の証明書なら誰でもロールを引き受けられる可能性**: `k8s-ca` は ClusterIssuer なので、どの Namespace からでも証明書を発行できる。ロールの信頼ポリシーで CN を絞っていなければ、どの証明書でも KppaasRole を使える

---

## 設計

利用者ごとに証明書・IAM ロールを分け、helper（`aws_signing_helper serve`）を**それぞれの Pod の中のサイドカー**にする。
共用の `aws-credential-helper`（DaemonSet・Service・Certificate と Argo CD の Application）は廃止する。

```
external-secrets コントローラーの Pod（このリポジトリ charts/external-secrets）
  ├─ aws-credential-helper（ネイティブサイドカー）
  │    証明書 CN=external-secrets → KappaasExternalSecretsRole（127.0.0.1:9911）
  └─ external-secrets … AWS_EC2_METADATA_SERVICE_ENDPOINT=http://127.0.0.1:9911

kakeibon-backend の Pod（kakeibon-apps リポジトリ）
  ├─ aws-credential-helper（ネイティブサイドカー）
  │    証明書 CN=kakeibon-backend → KakeibonBackendRole（127.0.0.1:9911）
  └─ backend … AWS_EC2_METADATA_SERVICE_ENDPOINT=http://127.0.0.1:9911
```

| 利用者                         | 証明書 CN（置き場所）                                  | IAM ロール（新規）           | 権限                                                         | 接続先                  |
| ------------------------------ | ------------------------------------------------------ | ---------------------------- | ------------------------------------------------------------ | ----------------------- |
| external-secrets コントローラー | `external-secrets`（external-secrets Namespace）       | `KappaasExternalSecretsRole` | Secrets Manager の読み取り（`kappaas-apps/*`、`kakeibon/*`） | `http://127.0.0.1:9911` |
| kakeibon-backend               | `kakeibon-backend`（kakeibon Namespace）               | `KakeibonBackendRole`        | DynamoDB `kakeibon-dev`、S3 の 2 バケット、SES の送信        | `http://127.0.0.1:9911` |

- トラストアンカーとプロファイルは今のものを使い続ける。プロファイルの roleArns に 2 ロールを追加する
- 信頼ポリシーで「トラストアンカー」と「証明書の CN」の両方を条件にする。これで、同じ CA の別の証明書では引き受けられない（問題点 3 の対策）
- 署名付き URL（S3）は、署名した一時資格情報の期限（既定 1 時間）を超えて使えない。今と同じ挙動

### サイドカーにする理由

- helper は Pod 内の `127.0.0.1` で待ち受けるだけなので、Service も NetworkPolicy も socat も不要。資格情報は Pod の外に出ない（問題点 1・2 の対策）
- 共用の helper（DaemonSet）と `charts/aws-credential-helper` を丸ごと廃止できる
- ネイティブサイドカー（`initContainers` に `restartPolicy: Always`。クラスタは v1.36 なので使える）にすると、helper の startupProbe が通るまで本体が起動しない。helper が落ちても kubelet が再起動する
- helper は Pod ごとに動く（external-secrets コントローラー 2、kakeibon-backend 2）。増える負荷は 1 つあたり CPU 10m・メモリ 32Mi 程度
- 証明書の更新は今と同じく自動。helper は資格情報を更新するたびに証明書ファイルを読み直す
- ESO の Helm チャート（2.5.0）は `extraInitContainers` / `extraVolumes` / `extraEnv` を持っており、`restartPolicy: Always` もそのまま描画される（`helm template` で確認）。サイドカーはコントローラーにだけ入り、webhook には入らない

### 調査結果（2026-11-14）

- ExternalSecret が読むシークレット（すべて external-secrets のコントローラーが取得する）
  - `kappaas-apps/repositories/kakeibon-apps/github-personal-access-token`
  - `kappaas-apps/cloudflare/credentials`
  - `kappaas-apps/ghcr/github-personal-access-token`
  - `kakeibon/jwt-secret`
- kakeibon-backend の AWS 利用（`kakeibon/backend/src/services/{dynamo,s3,ses}.ts`、ConfigMap `kakeibon-backend-config`）
  - DynamoDB テーブル `kakeibon-dev`（インデックスあり）: Get / Put / Delete / Query / Scan / BatchGet / BatchWrite / TransactWrite
    - TransactWrite は Put / Delete だけで構成され、ConditionCheck や Update は使っていない
  - S3 バケット `kakeibo-data-bucket`: GetObject / PutObject / DeleteObject / DeleteObjects、署名付き URL
    - `kakeibo-user-bucket` は使っていない（2026-11-17 確認）。KakeibonBackendRole（aws/rolesanywhere.yaml）には権限を付けない。下の権限ポリシーの例に残っている user バケットの行は、今の KppaasRole の内容
  - SES: `SendEmail`（送信元 `noreply@kawashima-kazuh.com`）
  - Secrets Manager は直接使っていない（JWT は ExternalSecret 経由）
- kakeibon-backend の Deployment には、どのコンテナにもマウントされていない volume `certs`（Secret `kakeibon-backend-secrets`、クラスタに存在しない）と `bin` が残っている。以前サイドカーを試したときの名残と思われる。今回のサイドカーで置き換える

---

## IAM ポリシー

### 信頼ポリシー（2 ロール共通。CN だけ変える）

`<CN>` は `external-secrets` または `kakeibon-backend`。

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": { "Service": "rolesanywhere.amazonaws.com" },
      "Action": ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"],
      "Condition": {
        "ArnEquals": {
          "aws:SourceArn": "arn:aws:rolesanywhere:ap-northeast-1:391726422976:trust-anchor/04787480-a940-4438-8b81-8551d90d2765"
        },
        "StringEquals": {
          "aws:PrincipalTag/x509Subject/CN": "<CN>"
        }
      }
    }
  ]
}
```

### KappaasExternalSecretsRole の権限ポリシー

現在の KppaasRole の `VisualEditor0` をそのまま使う。4 つの操作は、ESO のドキュメント（[AWS access](https://external-secrets.io/latest/provider/aws-access/)）が挙げている権限と同じ。

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadSecrets",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetResourcePolicy",
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret",
        "secretsmanager:ListSecretVersionIds"
      ],
      "Resource": [
        "arn:aws:secretsmanager:ap-northeast-1:391726422976:secret:kakeibon/*",
        "arn:aws:secretsmanager:ap-northeast-1:391726422976:secret:kappaas-apps/*"
      ]
    }
  ]
}
```

現在のポリシーから外すもの:

| 操作                                                  | 外す理由                                                                                                                             |
| ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `ListSecrets`、`BatchGetSecretValue`（VisualEditor1） | `dataFrom` や `find` を使っていない。また `ListSecrets` はリソースを指定できない操作なので、今のリソース指定では実際には効いていない |

将来 `dataFrom.find` を使う場合は、`ListSecrets` を `"Resource": "*"` で追加する。

### KakeibonBackendRole の権限ポリシー

現在の KppaasRole の DynamoDB / S3 / SES の文を移す。SES だけ送信元に絞る。

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "DynamoDB",
      "Effect": "Allow",
      "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Scan",
        "dynamodb:Query",
        "dynamodb:BatchWriteItem",
        "dynamodb:DescribeTable",
        "dynamodb:BatchGetItem"
      ],
      "Resource": [
        "arn:aws:dynamodb:ap-northeast-1:391726422976:table/kakeibon-dev",
        "arn:aws:dynamodb:ap-northeast-1:391726422976:table/kakeibon-dev/*"
      ]
    },
    {
      "Sid": "S3",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::kakeibo-data-bucket",
        "arn:aws:s3:::kakeibo-data-bucket/*",
        "arn:aws:s3:::kakeibo-user-bucket",
        "arn:aws:s3:::kakeibo-user-bucket/*"
      ]
    },
    {
      "Sid": "SES",
      "Effect": "Allow",
      "Action": ["ses:SendEmail", "ses:SendRawEmail"],
      "Resource": "arn:aws:ses:ap-northeast-1:391726422976:identity/kawashima-kazuh.com",
      "Condition": {
        "StringEquals": { "ses:FromAddress": "noreply@kawashima-kazuh.com" }
      }
    }
  ]
}
```

- DynamoDB と S3 は現在のポリシーと同じ。コードで使っている操作はすべて含まれている
  - `UpdateItem` と `DescribeTable` は今のコードでは使っていないが、動作を変えないために残している
  - TransactWrite は内部の Put / Delete の権限で評価されるので、追加の権限は不要
- SES は `"Resource": "*"` から、送信元ドメインの ID と送信元アドレスに絞った
  - **未確認**: SES に登録している ID がドメイン単位（`kawashima-kazuh.com`）か、メールアドレス単位（`noreply@kawashima-kazuh.com`）か。アドレス単位なら、Resource をその ID の ARN に変える
  - SES のコンソールの「Identities」で確認する

KppaasRole の今のポリシーは、最後（ステップ 5）にロールごと削除するまで変更しない。

---

## AWS 側の手順 [AWS]

`aws` CLI を使う場合の例。コンソールで作成してもよい。

```bash
# [Mac] 作業用ディレクトリ（ポリシー JSON を置く）
mkdir -p ~/k8s-backup/iam-split && cd ~/k8s-backup/iam-split
# 上の信頼ポリシーを CN ごとに trust-external-secrets.json / trust-kakeibon-backend.json として保存
# 権限ポリシーを perm-external-secrets.json / perm-kakeibon-backend.json として保存

# 1. ロールを作る
aws iam create-role --role-name KappaasExternalSecretsRole \
  --assume-role-policy-document file://trust-external-secrets.json
aws iam put-role-policy --role-name KappaasExternalSecretsRole \
  --policy-name KappaasExternalSecrets --policy-document file://perm-external-secrets.json

aws iam create-role --role-name KakeibonBackendRole \
  --assume-role-policy-document file://trust-kakeibon-backend.json
aws iam put-role-policy --role-name KakeibonBackendRole \
  --policy-name KakeibonBackend --policy-document file://perm-kakeibon-backend.json

# 2. プロファイルの現在の設定を確認
aws rolesanywhere get-profile --profile-id 591bccad-ba10-4397-b798-5de2d5795f17 \
  --query 'profile.{roles:roleArns,managed:managedPolicyArns,session:sessionPolicy,duration:durationSeconds}'
```

- `managedPolicyArns` や `sessionPolicy` が設定されている場合、実際に使える権限は「ロールの権限」と「プロファイルのポリシー」の重なりになる。新しいロールの権限がプロファイル側で削られないか確認する

```bash
# 3. プロファイルに 2 ロールを追加（既存の KppaasRole も残す）
aws rolesanywhere update-profile --profile-id 591bccad-ba10-4397-b798-5de2d5795f17 \
  --role-arns \
    arn:aws:iam::391726422976:role/KppaasRole \
    arn:aws:iam::391726422976:role/KappaasExternalSecretsRole \
    arn:aws:iam::391726422976:role/KakeibonBackendRole
```

---

## クラスタ側の変更

### external-secrets: charts/external-secrets（このリポジトリ）

> **実装済み（段階 1）**: 実際のファイルは `charts/external-secrets/templates/aws-certificate.yaml`・`templates/aws-config.yaml`・`values.yaml`・`{prod,dev}/values.yaml`。
> 下の例との違い: ARN を直接書かず ConfigMap `external-secrets-aws` から環境変数で渡す（`envFrom` と `sh -c 'exec aws_signing_helper serve ... "$ROLE_ARN"'`）。

**Certificate（新規）** `charts/external-secrets/templates/aws-certificate.yaml`

```yaml
# ESO コントローラーのサイドカー（aws_signing_helper）が IAM Roles Anywhere で使うクライアント証明書
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: external-secrets-aws
  namespace: {{ .Release.Namespace }}
spec:
  commonName: external-secrets
  dnsNames:
    - external-secrets
  secretName: external-secrets-aws-cert
  duration: 720h
  renewBefore: 72h
  privateKey:
    algorithm: RSA
    size: 2048
  usages:
    - digital signature
    - client auth
  issuerRef:
    name: k8s-ca
    kind: ClusterIssuer
```

**values の変更** `charts/external-secrets/values.yaml` の `external-secrets:` の下（変更・追加する部分だけ）

```yaml
external-secrets:
  extraEnv:
    - name: AWS_EC2_METADATA_SERVICE_ENDPOINT
      value: "http://127.0.0.1:9911"           # Pod 内のサイドカー
    - name: AWS_EC2_METADATA_SERVICE_ENDPOINT_MODE
      value: "IPv4"
  extraInitContainers:
    - name: download-helper
      image: alpine:3.20
      command:
        - sh
        - -c
        - |
          wget -qO /opt/bin/aws_signing_helper \
            https://rolesanywhere.amazonaws.com/releases/1.8.2/X86_64/Linux/Amzn2023/aws_signing_helper && \
          chmod +x /opt/bin/aws_signing_helper
      volumeMounts:
        - name: bin
          mountPath: /opt/bin
    - name: aws-credential-helper              # ネイティブサイドカー
      image: amazonlinux:2023
      restartPolicy: Always
      command:
        - /opt/bin/aws_signing_helper
        - serve
        - --certificate
        - /certs/tls.crt
        - --private-key
        - /certs/tls.key
        - --trust-anchor-arn
        - arn:aws:rolesanywhere:ap-northeast-1:391726422976:trust-anchor/04787480-a940-4438-8b81-8551d90d2765
        - --profile-arn
        - arn:aws:rolesanywhere:ap-northeast-1:391726422976:profile/591bccad-ba10-4397-b798-5de2d5795f17
        - --role-arn
        - arn:aws:iam::391726422976:role/KappaasExternalSecretsRole
        - --port
        - "9911"
      startupProbe:                            # 通るまで ESO 本体を起動しない
        exec:                                  # helper は 127.0.0.1 だけで待ち受けるので tcpSocket（Pod IP に接続）は使えない
          command: ["bash", "-c", "</dev/tcp/127.0.0.1/9911"]
        periodSeconds: 2
        failureThreshold: 30
      resources:
        requests:
          cpu: 10m
          memory: 32Mi
        limits:
          memory: 64Mi
      volumeMounts:
        - name: certs
          mountPath: /certs
          readOnly: true
        - name: bin
          mountPath: /opt/bin
  extraVolumes:
    - name: certs
      secret:
        secretName: external-secrets-aws-cert
    - name: bin
      emptyDir: {}
```

- `extraVolumeMounts` は ESO 本体のコンテナ用なので使わない（サイドカーの volumeMounts はコンテナ定義に直接書く）
- webhook と certController は AWS を使わないので変更しない（`extraInitContainers` などはコントローラーにだけ入る）
- 2026-11-14 に、この内容を `charts/external-secrets` のコピーに当てて `helm template` と `kubectl apply --dry-run=server` で確認済み（サイドカーはコントローラーの Deployment にだけ入る）

### kakeibon-backend: kakeibon-apps リポジトリ

**Certificate（新規）** `components/backend/certificate.yaml`。`components/backend/kustomization.yaml` の `resources` に追加する。

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: kakeibon-backend-aws
  namespace: kakeibon
spec:
  commonName: kakeibon-backend
  dnsNames:
    - kakeibon-backend
  secretName: kakeibon-backend-aws-cert
  duration: 720h
  renewBefore: 72h
  privateKey:
    algorithm: RSA
    size: 2048
  usages:
    - digital signature
    - client auth
  issuerRef:
    name: k8s-ca
    kind: ClusterIssuer
```

**Deployment の変更** `components/backend/deployment.yaml`（変更する部分だけ）

```yaml
    spec:
      initContainers:
        - name: download-helper                # helper を取得
          image: alpine:3.20
          command:
            - sh
            - -c
            - |
              wget -qO /opt/bin/aws_signing_helper \
                https://rolesanywhere.amazonaws.com/releases/1.8.2/X86_64/Linux/Amzn2023/aws_signing_helper && \
              chmod +x /opt/bin/aws_signing_helper
          volumeMounts:
            - name: bin
              mountPath: /opt/bin
        - name: aws-credential-helper          # ネイティブサイドカー
          image: amazonlinux:2023
          restartPolicy: Always
          command:
            - /opt/bin/aws_signing_helper
            - serve
            - --certificate
            - /certs/tls.crt
            - --private-key
            - /certs/tls.key
            - --trust-anchor-arn
            - arn:aws:rolesanywhere:ap-northeast-1:391726422976:trust-anchor/04787480-a940-4438-8b81-8551d90d2765
            - --profile-arn
            - arn:aws:rolesanywhere:ap-northeast-1:391726422976:profile/591bccad-ba10-4397-b798-5de2d5795f17
            - --role-arn
            - arn:aws:iam::391726422976:role/KakeibonBackendRole
            - --port
            - "9911"
          startupProbe:                        # 通るまで backend を起動しない
            exec:                              # helper は 127.0.0.1 だけで待ち受けるので tcpSocket（Pod IP に接続）は使えない
              command: ["bash", "-c", "</dev/tcp/127.0.0.1/9911"]
            periodSeconds: 2
            failureThreshold: 30
          resources:
            requests:
              cpu: 10m
              memory: 32Mi
            limits:
              memory: 64Mi
          volumeMounts:
            - name: certs
              mountPath: /certs
              readOnly: true
            - name: bin
              mountPath: /opt/bin
      containers:
        - name: backend
          env:
            - name: AWS_EC2_METADATA_SERVICE_ENDPOINT
              value: "http://127.0.0.1:9911"         # Pod 内のサイドカー
            - name: AWS_EC2_METADATA_SERVICE_ENDPOINT_MODE
              value: "IPv4"
      volumes:                                 # 残っている certs / bin を置き換える
        - name: certs
          secret:
            secretName: kakeibon-backend-aws-cert
        - name: bin
          emptyDir: {}
```

- `JWT_SECRET` などの既存の env、probe、resources はそのまま
- 2026-11-14 に、今の Deployment にこの変更を当てたものと Certificate を `kubectl apply --dry-run=server` で確認済み

### 廃止するもの（移行の最後）

- `applications/base/aws-credential-helper.yaml` と `applications/base/kustomization.yaml` のその行（finalizer があるので、Argo CD が DaemonSet・Service・Certificate を削除する）
- `charts/aws-credential-helper/`
- `.github/dependabot.yml` の docker の `/charts/aws-credential-helper`

---

## 移行手順（本番を止めずに切り替える）

旧 helper（DaemonSet）はステップ 4 まで動かしたままにする。ステップ 2・3 で問題が出ても、revert すれば旧 helper に戻る。

### 1. AWS: 2 ロールを作成し、プロファイルに追加 [AWS]

上の「AWS 側の手順」を実施する。KppaasRole はこの時点では残す。

- [ ] KappaasExternalSecretsRole 作成
- [ ] KakeibonBackendRole 作成
- [ ] プロファイルの roleArns が 3 つ

### 2. external-secrets にサイドカーを追加 [Mac]

上の「external-secrets: charts/external-secrets」の変更（Certificate の追加と values の変更）をして push する。接続先も同時に `127.0.0.1:9911` に変わる。

```bash
# 事前に差分を確認（コントローラーの Deployment と新しい Certificate だけが変わること）
cd charts/external-secrets && helm dependency build >/dev/null && \
  helm template external-secrets . -n external-secrets | kubectl diff --server-side -f - ; \
  rm -rf charts Chart.lock; cd -

# 同期後
kubectl -n external-secrets get certificate external-secrets-aws        # READY True
kubectl -n external-secrets get secret external-secrets-aws-cert -o jsonpath='{.data.tls\.crt}' \
  | base64 -d | openssl x509 -noout -subject                            # CN = external-secrets
kubectl -n external-secrets rollout status deploy/external-secrets
kubectl -n external-secrets get pods -l app.kubernetes.io/name=external-secrets   # READY 2/2

# サイドカーが新しいロールを引き受けていること（ロール名が表示される）
kubectl -n external-secrets exec deploy/external-secrets -c aws-credential-helper -- sh -c '
  T=$(curl -s -X PUT http://127.0.0.1:9911/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
  curl -s -H "X-aws-ec2-metadata-token: $T" http://127.0.0.1:9911/latest/meta-data/iam/security-credentials/; echo'

# 全 ExternalSecret を再取得させる
kubectl annotate externalsecret -A --all force-sync=$(date +%s) --overwrite
kubectl get secretstore -A
kubectl get externalsecret -A
```

- [ ] Certificate が Ready、CN が `external-secrets`
- [ ] コントローラーの Pod が 2/2 Running、ロール名が `KappaasExternalSecretsRole`
- [ ] SecretStore 3 つが Ready
- [ ] ExternalSecret 4 つが SecretSynced
- [ ] ESO のログに AccessDenied が出ていない（`kubectl -n external-secrets logs deploy/external-secrets -c external-secrets --since=10m | grep -i denied`）

### 3. kakeibon-backend にサイドカーを追加 [Mac]

kakeibon-apps リポジトリで、上の「kakeibon-backend: kakeibon-apps リポジトリ」の変更（Certificate の追加、Deployment の変更、kustomization への登録）をして push する。

```bash
kubectl -n kakeibon get certificate kakeibon-backend-aws          # READY True
kubectl -n kakeibon get secret kakeibon-backend-aws-cert -o jsonpath='{.data.tls\.crt}' \
  | base64 -d | openssl x509 -noout -subject                      # CN = kakeibon-backend
kubectl -n kakeibon rollout status deploy/kakeibon-backend
kubectl -n kakeibon get pods -l app=kakeibon-backend              # READY 2/2

# サイドカーが新しいロールを引き受けていること（ロール名が表示される）
kubectl -n kakeibon exec deploy/kakeibon-backend -c aws-credential-helper -- sh -c '
  T=$(curl -s -X PUT http://127.0.0.1:9911/latest/api/token -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
  curl -s -H "X-aws-ec2-metadata-token: $T" http://127.0.0.1:9911/latest/meta-data/iam/security-credentials/; echo'
```

確認（ブラウザで kakeibon を操作する）:

- [ ] Certificate が Ready、CN が `kakeibon-backend`
- [ ] Pod が 2/2 Running、ロール名が `KakeibonBackendRole`
- [ ] ログインできる
- [ ] データの表示・登録・削除ができる（DynamoDB）
- [ ] ファイルのアップロード・表示ができる（S3、署名付き URL）
- [ ] メールが届く（SES）
- [ ] backend のログに AccessDenied が出ていない（`kubectl -n kakeibon logs deploy/kakeibon-backend -c backend --since=30m | grep -i denied`）

### 4. aws-credential-helper を廃止 [Mac]

上の「廃止するもの」を削除して push する。Argo CD が Application `aws-credential-helper` を削除し、finalizer によって DaemonSet・Service・Certificate も削除される。

```bash
kubectl get ns aws-credential-helper                    # 中身が空になる（Namespace 自体は残るので、空なら手で削除してよい）
kubectl -n aws-credential-helper get all

# LAN から 9911 番ポートに届かないこと（000 = 応答なし）
for ip in 192.168.3.124 192.168.3.125 192.168.3.126 192.168.3.127; do
  curl -s -o /dev/null -w "$ip: %{http_code}\n" --max-time 3 -X PUT http://$ip:9911/latest/api/token \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60'
done
```

- [ ] aws-credential-helper の DaemonSet・Service・Certificate が消えた
- [ ] 稼働中のノードがすべて 9911 に応答しない（`000`）
- [ ] ステップ 2・3 の確認項目がすべて OK のまま

### 5. KppaasRole を削除 [AWS]（ステップ 4 から 1 週間ほど様子を見てから）

```bash
aws rolesanywhere update-profile --profile-id 591bccad-ba10-4397-b798-5de2d5795f17 \
  --role-arns \
    arn:aws:iam::391726422976:role/KappaasExternalSecretsRole \
    arn:aws:iam::391726422976:role/KakeibonBackendRole

# CloudTrail で KppaasRole が使われていないことを確認してから削除
aws iam list-role-policies --role-name KppaasRole
aws iam delete-role-policy --role-name KppaasRole --policy-name <上で表示された名前>
aws iam delete-role --role-name KppaasRole
```

- [ ] プロファイルの roleArns が 2 つ
- [ ] KppaasRole 削除

---

## 戻し方

| 戻すタイミング                       | 手順                                                                                                                         |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------- |
| ステップ 2 の後（ステップ 4 より前） | charts/external-secrets の変更を revert して push。接続先が旧 Service に戻る。旧 helper はまだ動いている                      |
| ステップ 3 の後（ステップ 4 より前） | kakeibon-apps の変更を revert して push。接続先が旧 Service に戻る。旧 helper はまだ動いている                                |
| ステップ 4 の後                      | ステップ 4 の変更を revert して旧 helper を戻してから、上の手順で接続先を戻す（KppaasRole を削除する前に限る）               |
| ステップ 5 の後                      | 新しいロールのポリシーを修正する（KppaasRole は無いので戻せない）                                                             |

---

## 未確認事項

- [x] SES の ID がドメイン単位（`kawashima-kazuh.com`）かアドレス単位（`noreply@kawashima-kazuh.com`）か → **ドメイン単位**（2026-11-16 確認。スタック `kakeibon-ses`）。KakeibonBackendRole の SES の Resource は `identity/kawashima-kazuh.com` のまま
- [ ] Roles Anywhere プロファイルに `managedPolicyArns` / `sessionPolicy` が設定されていないか（設定されていれば、新ロールの権限がそれで削られないか）

---

## 関連する改善・注意点（今回の範囲外）

- **helper の取得**: 各サイドカーの initContainer は、Pod が起動するたびに `aws_signing_helper` をインターネットから取得する（チェックサム確認なし）。取得できないと ESO コントローラーや kakeibon-backend も起動しない。SHA256 の確認を入れるか、helper を含むイメージを使う
- **Dependabot**: サイドカーのイメージ（`alpine:3.20`、`amazonlinux:2023`）は、external-secrets では Helm の values.yaml の中に書くことになる。values.yaml はマニフェストとして解釈されないので、Dependabot は更新できない可能性がある。kakeibon-apps の Deployment に書いたものは更新対象になる
- **SecretStore ごとの権限分離**: 今は、どの Namespace の SecretStore でも、ESO コントローラーのロール（`kappaas-apps/*` と `kakeibon/*` を読める）がそのまま使われる。
  ESO の `spec.provider.aws.role`（SecretStore ごとに別ロールを AssumeRole する機能）を使えば、たとえば kakeibon の SecretStore は `kakeibon/*` だけを読めるようにできる。
  その場合、コントローラーのロールには `sts:AssumeRole` / `sts:TagSession` だけを持たせ、Secrets Manager の権限は SecretStore ごとのロールに移す
- **ExternalSecret の更新**: kakeibon の 3 つの ExternalSecret（`kakeibon-apps-secret`、`kakeibon-backend-secret`、`kakeibon-ghcr-secret`）は `refreshPolicy: OnChange` のため、Secrets Manager の値を変えても自動では取り込まれない（`refreshInterval: 1h` は効いていない）
