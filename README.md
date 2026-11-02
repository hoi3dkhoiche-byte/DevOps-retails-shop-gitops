# Retail Store GitOps trên AWS

Repo này lưu trạng thái triển khai cho EKS `retail-lab-eks` tại Singapore: platform, hai môi trường `retail-dev`/`retail-prod` và phiên bản image của năm service. Terraform ở [repo source](https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop/tree/main/infra/retail-lab) tạo tài nguyên AWS; Argo CD đọc repo này để triển khai Kubernetes.

## Trạng thái ban đầu

- Dev đã có đủ năm image từ [Retail CI 41033493219, attempt 1](https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop/actions/runs/41033493219/attempts/1), source commit `d0e13455270aed05c1029422366de96873524edf`. Toàn bộ 14 job của lần chạy này thành công, gồm E2E, scan và publish.
- [Manifest gốc và bằng chứng](releases/41033493219-1/) lưu schema, source SHA, run/attempt, checksum artifact và kết quả đối chiếu manifest/config ID trên registry. Backend profile là `mysql-redis-opensearch`, platform `linux/amd64`.
- [Dev images](environments/dev/images.yaml) dùng digest bất biến. Chart ứng dụng được đọc từ chính source commit của release. Không build lại image trong repo này.
- [Production images](environments/prod/images.yaml) đang tắt, chờ nghiệm thu dev trên EKS. Chưa có bằng chứng `accepted.json` vì chưa chạy dev trên AWS.
- `deployment.enabled: false` trong [config.yaml](config.yaml): cả bốn chart nội bộ chưa sinh tài nguyên. Phải nhập output Terraform thật và kích hoạt trước khi bootstrap. Push repo không tạo AWS resource hoặc triển khai vào cluster.

| Service | Docker Hub repository |
| --- | --- |
| UI | `hmannnguye/devops-retails-shop` |
| Catalog | `hmannnguye/devops-retails-shop-catalog` |
| Cart | `hmannnguye/devops-retails-shop-cart` |
| Checkout | `hmannnguye/devops-retails-shop-checkout` |
| Orders | `hmannnguye/devops-retails-shop-orders` |

## Các file cần đọc

| Đường dẫn | Vai trò |
| --- | --- |
| [docs/aws-architecture.md](docs/aws-architecture.md) | Cấu hình AWS đề xuất theo bản thiết kế và Terraform: node, database, Redis, MQ, OpenSearch, mạng và giới hạn của lab |
| [config.yaml](config.yaml), [versions.yaml](versions.yaml) | Đích triển khai, IAM/SG/bucket và phiên bản platform đã khóa |
| [argocd/retail-root.yaml](argocd/retail-root.yaml) | Application gốc trỏ đến repo này, nhánh `main`, chart `bootstrap` |
| [bootstrap/](bootstrap/) | Namespace, AppProject, storage và các Application con |
| [applications/](applications/) | Năm Application theo môi trường, Secrets Manager/ESO, NetworkPolicy, Ingress, public RDS CA |
| [environments/](environments/) | Values riêng cho từng service/môi trường và bộ digest |
| [platform/](platform/), [platform-resources/](platform-resources/) | Controllers, Prometheus/Grafana, Loki/Alloy, dashboard, storage và Velero |
| [argocd-secrets/](argocd-secrets/) | Tham chiếu credential Argo CD từ Secrets Manager qua ESO |
| [scripts/prepare.py](scripts/prepare.py), [scripts/bootstrap-argocd.ps1](scripts/bootstrap-argocd.ps1) | Nhập cấu hình AWS thật, kiểm tra đích, khởi tạo ESO rồi Argo CD |
| [scripts/release/](scripts/release/) | Kiểm tra release, nghiệm thu dev và hợp đồng promote |
| [.github/workflows/validate.yml](.github/workflows/validate.yml) | Kiểm tra PR/push: release, test và render Helm; không truy cập AWS |

## Tạo hạ tầng và kích hoạt lần đầu

1. Trong **repo source**, làm theo [hướng dẫn hạ tầng](https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop/blob/main/scripts/infra/README.md): chuẩn bị state/IAM/platform secret, chạy preflight Singapore, xem Terraform plan rồi apply. Phiên bản engine, EKS/add-on và instance phải được AWS xác nhận khả dụng trước apply. Bản cấu hình này chưa phải bằng chứng AWS đã chạy.
2. Chạy backend bootstrap từ trong VPC theo hướng dẫn trên. Bước này tạo database/user cho từng service/môi trường, RabbitMQ vhost/user, OpenSearch role/index và ghi credential vào Secrets Manager. Hoàn thành bước này trước khi bật ứng dụng.
3. Clone repo GitOps cạnh repo source, cài Python 3.12+ và Helm 3.19.0. Các lệnh dưới đây chạy từ **repo GitOps**; điều chỉnh đường dẫn source nếu tên thư mục khác:

   ```powershell
   python -m pip install -r requirements-gitops.txt
   New-Item -ItemType Directory -Force .local | Out-Null
   terraform -chdir=../DevOps-retails-shop/infra/retail-lab output -json | Set-Content -Encoding utf8 .local/terraform-outputs.json
   python scripts/prepare.py --terraform-output .local/terraform-outputs.json
   ```

   Script chỉ lấy ID/ARN, bucket và cấu hình định tuyến; không lấy mật khẩu. Không commit output Terraform, state, kubeconfig hoặc secret. Lần nhập đầu vẫn giữ `deployment.enabled: false` để xem diff.
4. Kiểm tra `config.yaml` và các values trong `platform/`, sau đó kích hoạt và kiểm tra:

   ```powershell
   python scripts/prepare.py --activate
   python scripts/prepare.py --check
   python scripts/check-release.py
   python validate.py --source ../DevOps-retails-shop/retail-store-sample-app
   ```

   Tài khoản, VPC, SG, mười IAM role, bucket và IP quản trị `/32` phải hợp lệ. Không tự thay placeholder bằng ID ví dụ. Source checkout dùng để kiểm tra phải ở commit của release; workflow tự checkout đúng SHA. Nếu dev/prod đang dùng source SHA khác nhau, cung cấp thêm `--prod-source` trỏ đến checkout riêng của prod.
5. Commit/merge cấu hình đã kích hoạt vào `main`, để Argo CD đọc đúng nội dung vừa kiểm tra. Từ máy quản trị có AWS CLI, Helm, kubectl và quyền EKS Access Entry tương ứng, chạy:

   ```powershell
   ./scripts/bootstrap-argocd.ps1 -AwsProfile retail-admin
   ```

   Thay `retail-admin` bằng AWS profile thực tế. Script kiểm tra account, cluster ARN/version/API endpoint và quyền quản trị; dùng kubeconfig riêng `.local/kubeconfig.yaml`. Nó cài ESO, chờ Secret Argo CD đồng bộ từ AWS, cài Argo CD rồi tạo `retail-root`. Nếu repository đổi sang private, phải cấu hình quyền đọc Git cho Argo CD trước khi đồng bộ.

Namespace/AppProject và storage được tạo trước các Application phụ thuộc. ESO, Rollouts, credential monitoring và Prometheus CRD có thứ tự đồng bộ riêng. Root/platform không tự prune. Dev tự sync; production workload phải sync thủ công.

## Nối CI với GitOps và promote

Trong **repo source** `DevOps-retails-shop`, vào **Settings → Secrets and variables → Actions** và đặt:

| Loại | Tên | Giá trị/quyền |
| --- | --- | --- |
| Variable | `GITOPS_ENABLED` | `true` |
| Secret | `GITOPS_TOKEN` | Token được giới hạn ở repo GitOps, quyền Contents và Pull requests: read/write |

Workflow [Retail GitOps](https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop/blob/main/.github/workflows/gitops.yml) đã có trong source. Sau một lần Retail CI thành công trên `main`, nó xác minh đầy đủ run/attempt và mở PR cập nhật dev. Merge PR sẽ khiến Argo CD tự sync khi hạ tầng đã kích hoạt. Docker Hub credential vẫn ở workflow CI source; repo GitOps không push image và không cần Docker Hub write token.

Khi dev đã hoạt động và UI hoàn tất Blue/Green, chạy nghiệm thu từ repo này:

```powershell
python scripts/release/accept_dev.py --images environments/dev/images.yaml --output environments/dev/accepted.json --context retail-lab-eks --operator YOUR_NAME
```

Nếu dùng kubeconfig riêng của bootstrap, đặt `$env:KUBECONFIG` tới đường dẫn tuyệt đối `.local/kubeconfig.yaml` trước khi chạy. Helper kiểm tra workload, digest/image ID thực tế, UI active và smoke API; cần kubectl, Docker buildx và quyền đọc registry. Review/commit `accepted.json`, rồi chạy thủ công workflow **Retail GitOps** ở repo source với `expected_dev_commit` bằng full SHA đã nghiệm thu. PR production dùng đúng bộ năm digest đó. Sau merge, sync production và promote UI thủ công; không build một bộ image mới.

Để rollback, revert thay đổi image về bộ digest đã nghiệm thu trước đó và sync; UI đang chờ promote có thể abort Rollout. Xem thêm [release helpers](scripts/release/README.md). Không dùng `deployment.enabled: false` như cơ chế rollback: các tài nguyên đã tồn tại không tự bị xóa khi root không prune.

## Truy cập và vận hành

Ứng dụng dùng Service/container port `8080`. Khi kubeconfig riêng đã được chọn:

```powershell
kubectl --context retail-lab-eks -n retail-dev port-forward service/ui-active 8080:8080
kubectl --context retail-lab-eks -n retail-prod port-forward service/ui-preview 8081:8080
kubectl --context retail-lab-eks -n argocd port-forward service/argocd-server 8443:443
kubectl --context retail-lab-eks -n monitoring port-forward service/kube-prometheus-stack-grafana 3000:80
```

Khi có domain/ACM, `dev.retail.<domain>` và `retail.<domain>` dùng ALB HTTPS chung; dev giới hạn IP quản trị. Khi chưa có domain, dev truy cập bằng port-forward; HTTP ALB của production lab chỉ cho phép IP quản trị và chỉ xuất hiện khi production release bật. Route 53 alias cần trỏ đến ALB do controller tạo.

Credential đi từ Secrets Manager → ESO → Kubernetes Secret. Sau khi rotate credential, cập nhật `secretRevision` trong values để restart ứng dụng đọc biến môi trường. File PEM/PKCS12 kèm repo chỉ chứa CA công khai; `changeit` của truststore không bảo vệ private key hay mật khẩu ứng dụng.

Prometheus giữ 7 ngày/24GB trên PVC 30Gi. Loki dùng S3, giữ 7 ngày; Alloy thu log theo node. Velero backup Kubernetes resource 14 ngày, loại Secrets; RDS/Redis dùng backup riêng của AWS. Email/Telegram alert để tắt cho tới khi có receiver thật: thêm cấu hình vào `/retail/platform/alertmanager/config`, chạy prepare với `--terraform-output ... --enable-alerting` rồi review diff. RPO/RTO, failover, rotation và restore phải được đo trên AWS.

## Kiểm tra trước merge

```powershell
python scripts/check-release.py
python -B -m unittest discover -s scripts/release -p 'test_*.py' -v
python -B -m unittest discover -s tests -p 'test_*.py' -v
python validate.py --source ../DevOps-retails-shop/retail-store-sample-app
```

Workflow chạy các kiểm tra này trên Linux với chart đúng source SHA. Khi AWS chưa kích hoạt, validator xác minh chart thật không sinh resource, từ chối kích hoạt với placeholder, rồi dùng cấu hình thử tạm thời để render đầy đủ; không ghi ID thử vào `config.yaml`. Kiểm tra offline không thay thế nghiệm thu trên EKS. Khi cấu hình branch protection, dùng check `validate` của **GitOps validation** làm required check cho `main`.
