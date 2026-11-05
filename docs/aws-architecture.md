# Cấu hình AWS cho Retail GitOps

Các thông số đầy đủ dưới đây thuộc profile `architecture`. Repo hiện mặc định profile `lab` cho vòng tạo → kiểm tra → xóa, với node nhỏ và chỉ những controller cần cho dev; xem [hai profile và budget management](deployment-profiles.md). Profile lab không bật production, Loki/Alloy, Velero hoặc autoscaler, và metrics không lưu lâu dài.

Phương án này triển khai bản thiết kế `Thiết kế kiến trúc chi tiết.docx` và hai quyết định đã chốt: **một RDS MySQL, tách database/user theo service và môi trường; giữ OpenSearch riêng cho tìm kiếm**. Các thông số dưới đây khớp code Terraform và values ứng dụng hiện có. Đây là cấu hình khởi đầu cho lab; chưa phải kết quả triển khai hay nghiệm thu AWS.

Repo GitOps lưu cấu hình Kubernetes, phiên bản platform và digest ứng dụng. Terraform nằm ở [`infra/retail-lab` trong repo source](https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop/tree/main/infra/retail-lab). Không tạo thêm một bộ Terraform trong repo này.

`deployment.enabled: false` trong [`config.yaml`](../config.yaml) giữ các chart điều phối ở trạng thái chưa kích hoạt. Dev đã có bộ image CI xác minh; prod chưa có release được nghiệm thu. Việc push repo này chưa tạo AWS resource hoặc cài Argo CD.

## Mạng và EKS

| Thành phần | Cấu hình trong code |
| --- | --- |
| Region / AZ | `ap-southeast-1`; AZ `1a`, `1b`, `1c` |
| VPC | `10.0.0.0/16`, bật DNS support/hostnames |
| Public subnet | `10.0.10.0/24`, `10.0.20.0/24`, `10.0.30.0/24` |
| App subnet | `10.0.40.0/22`, `10.0.60.0/22`, `10.0.80.0/22`; worker không có public IP |
| Data subnet | `10.0.100.0/24`, `10.0.110.0/24`, `10.0.120.0/24`; chỉ route nội bộ VPC |
| Internet / NAT | Một Internet Gateway; một NAT Gateway ở public subnet AZ-a cho ba app subnet |
| EKS | `retail-lab-eks`, mục tiêu Kubernetes `1.36`; AL2023 x86_64, worker On-Demand |
| Kubernetes API | Private bật; public bật và chỉ nhận IP quản trị thực tế dạng `/32` |
| Quản trị | IAM role qua EKS Access Entry; role quản trị có cluster admin |
| Managed add-on | VPC CNI, CoreDNS, kube-proxy, EBS CSI; khóa đầy đủ phiên bản `eksbuild` |
| CNI | Bật NetworkPolicy và Pod Security Groups, enforcement `standard` |

Phiên bản `1.36` là mục tiêu đang khóa trong code, **phải được API của AWS xác nhận còn standard support tại Singapore trước khi plan/apply**. Nếu không có, cần PR đồng bộ EKS, add-on, Autoscaler và validator; không chỉ thay version lúc tạo cluster. AWS cung cấp `describe-cluster-versions` để kiểm tra vòng đời thực tế. [EKS version lifecycle](https://docs.aws.amazon.com/eks/latest/userguide/kubernetes-versions.html)

| Node group | Instance | Root disk gp3 | Min / desired / max | Vị trí |
| --- | --- | --- | --- | --- |
| management | `m6i.xlarge` — 4 vCPU / 16 GiB | 80 GiB | 1 / 1 / 2 | App subnet AZ-a |
| dev | `m6i.large` — 2 vCPU / 8 GiB | 50 GiB | 1 / 1 / 2 | Ba app subnet |
| prod | `m6i.large` — 2 vCPU / 8 GiB | 50 GiB | 1 / 1 / 3 | Ba app subnet |

Label/taint `workload=management|dev|prod` và affinity/toleration giữ đúng nơi chạy. Autoscaler điều chỉnh desired size; Terraform không ép lại số node sau mỗi apply. Monitoring dùng EBS tại AZ-a nên management được giữ ở AZ này.

Một NAT giúp giảm số gateway cho lab nhưng tạo phụ thuộc egress vào AZ-a, đồng thời có thể phát sinh phí truyền dữ liệu giữa AZ. Nếu nâng mục tiêu chịu lỗi toàn hệ thống, cần NAT theo AZ và route tương ứng. [AWS NAT gateway availability](https://docs.aws.amazon.com/vpc/latest/userguide/nat-gateway-basics.html)

## Backend managed và dữ liệu

| Thành phần | Cấu hình khởi đầu đúng với Terraform | Tách dev / prod |
| --- | --- | --- |
| RDS MySQL | `8.4.11`, `db.t4g.small`, Single-AZ, gp3 30 GiB tự tăng tối đa 100 GiB, mã hóa, private, TLS bắt buộc | Sáu database/user riêng: `catalog_dev`, `cart_dev`, `orders_dev`, `catalog_prod`, `cart_prod`, `orders_prod` |
| ElastiCache Redis OSS | `7.1`, một `cache.t4g.small`, TLS bắt buộc, mã hóa, chưa bật Multi-AZ/failover | ACL `checkout-dev` / `checkout-prod`; key `retail:dev:*` / `retail:prod:*`; default user bị vô hiệu |
| OpenSearch | `OpenSearch_3.3`, một `r8g.medium.search`, gp3 50 GiB, một data subnet AZ-a, HTTPS, mã hóa đĩa và giữa node | User/role riêng, index `products-dev` / `products-prod` |
| Amazon MQ RabbitMQ | `4.2`, `mq.m7g.large`, `CLUSTER_MULTI_AZ`, ba data subnet, private AMQPS `5671` | Vhost `retail-dev` / `retail-prod`, user Orders riêng và quyền giới hạn queue/exchange |

MySQL là dữ liệu gốc của Catalog, Cart và Orders. Catalog reindex đọc MySQL rồi ghi OpenSearch; Redis phục vụ Checkout. Không cần đổi backend sang PostgreSQL. Shared instance tiết kiệm số tài nguyên nhưng dev/prod vẫn chia sẻ CPU, RAM, dung lượng và phạm vi sự cố; database/user riêng không tạo cách ly về hiệu năng.

RDS hiện khóa minor version, không tự nâng minor; bật deletion protection, final snapshot và backup 7 ngày. Có thể đặt `mysql_multi_az=true` và đổi `mysql_instance_class` trong tfvars khi nâng cấu hình. RDS Multi-AZ bổ sung khả năng failover, còn kích thước instance cần quyết định từ số kết nối, CPU, RAM và load test. [RDS for MySQL](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_MySQL.html)

RabbitMQ là backend được yêu cầu HA ngay từ đầu. `mq.m7g.large` ở chế độ cluster tương ứng ba broker, không phải một broker; code không tùy chỉnh dung lượng đĩa nên dùng mặc định dịch vụ, hiện là 15 GB mỗi node cho cỡ này. Các queue ứng dụng dùng quorum và cần nghiệm thu failover. Amazon MQ được bật tự nâng minor theo cấu hình dịch vụ. [MQ instance types](https://docs.aws.amazon.com/amazon-mq/latest/developer-guide/rmq-broker-instance-types.html), [MQ cluster deployment](https://docs.aws.amazon.com/amazon-mq/latest/developer-guide/rabbitmq-broker-architecture.html)

OpenSearch R8g yêu cầu gp3, phù hợp loại đĩa đã chọn. Phải xác minh `r8g.medium.search`, OpenSearch `3.3` và giới hạn volume khả dụng trong tài khoản/region trước khi apply. [OpenSearch instance constraints](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/supported-instance-types.html)

Security Group backend mở MySQL `3306`, Redis `6379`, OpenSearch `443` từ node SG; NetworkPolicy giới hạn thêm theo service. MQ `5671` chỉ nhận Orders pod SG; management API MQ `443` chỉ mở cho app subnet quản trị. Ứng dụng xác minh chứng chỉ/hostname khi kết nối TLS; public RDS CA trong repo không phải credential.

## Workload Kubernetes

Hai namespace là `retail-dev` và `retail-prod`, AppProject riêng, NetworkPolicy default deny. Tất cả application Service và container dùng cổng `8080`. Giá trị tài nguyên đã có trong [`environments`](../environments):

| Service | Dev request CPU / RAM | Dev limit CPU / RAM | Prod request CPU / RAM | Prod limit CPU / RAM |
| --- | --- | --- | --- | --- |
| UI | 200m / 512Mi | 1000m / 1Gi | 250m / 512Mi | 1000m / 1Gi |
| Catalog | 100m / 128Mi | 500m / 256Mi | 100m / 128Mi | 500m / 256Mi |
| Cart | 200m / 512Mi | 1000m / 1Gi | 250m / 512Mi | 1000m / 1Gi |
| Checkout | 100m / 128Mi | 500m / 256Mi | 100m / 256Mi | 500m / 512Mi |
| Orders | 200m / 512Mi | 1000m / 1Gi | 250m / 768Mi | 1000m / 1536Mi |

UI dùng Rollout Blue/Green, `ui-active` và `ui-preview`, preview một replica, giữ bản cũ 120 giây, lịch sử ba revision. Dev tự promote; prod promote thủ công. HPA UI CPU 70% request, min 1, max 3 ở dev / 7 ở prod. Bốn API service bắt đầu một replica, RollingUpdate. HPA có thể làm tồn tại đồng thời nhiều pod của hai phiên bản UI; cần quan sát Pending/Quota khi thử tải.

Prod là tên môi trường triển khai trong lab. Một worker và một replica/service chưa đáp ứng HA ứng dụng. Nâng lên mục tiêu chịu lỗi cần ít nhất hai worker/replica phù hợp, phân tán node/AZ và kiểm tra cả backend/monitoring/egress.

Nếu có domain: một ALB public dùng group `retail-public`; `dev.retail.<domain>` và `retail.<domain>`, ACM cùng region, HTTPS và redirect HTTP. Rule dev giới hạn source IP quản trị. Nếu chưa có domain: prod dùng ALB HTTP giới hạn IP; dev, UI preview, Argo CD và Grafana dùng port-forward. Ingress prod chỉ được tạo khi release prod đã bật; cấu hình ban đầu chưa có Ingress này. AWS Load Balancer Controller tạo ALB; không tạo thêm ALB bằng Terraform. Route 53 alias đến ALB cần cấu hình sau khi có hostname thực tế.

## Platform, secret và backup

Terraform quản lý AWS, IAM và bốn EKS managed add-on. Script bootstrap cài ESO trước, chờ Secret của Argo CD được đồng bộ rồi cài Argo CD và root Application. Người vận hành cài/nâng cấp Argo CD bằng script và [`platform/argo-cd.yaml`](../platform/argo-cd.yaml), dùng phiên bản khóa trong [`versions.yaml`](../versions.yaml). Argo CD quản lý chín chart còn lại: Rollouts, Load Balancer Controller, ESO, Metrics Server, Autoscaler, Prometheus/Grafana/Alertmanager, Loki, Alloy và Velero. Autoscaler dùng image override `v1.36.1` tương ứng minor EKS dự kiến.

Secrets Manager → ESO → Kubernetes Secret → ứng dụng. Dev/prod/monitoring/Argo CD có IRSA reader riêng; không đưa mật khẩu hay AWS access key vào GitOps. Namespace SecretStore refresh mỗi 5 phút. Sau khi đổi credential và ESO đồng bộ xong, đổi `secretRevision` để các tiến trình đọc biến môi trường khởi động lại.

Khi source hoặc GitOps chuyển private, bật `repositoryCredentials.enabled` và dùng ESO đồng bộ GitHub credential read-only từ `/retail/platform/argocd/repositories`. Một Secret `repo-creds` theo prefix `https://github.com/hoi3dkhoiche-byte/` cấp quyền đọc cả chart source và values GitOps; bootstrap chờ Secret này trước root Application. Token được operator điền vào AWS, không do backend Job tạo. Repo public giữ tùy chọn này tắt. Xem [hướng dẫn hai repo private](../README.md#quyền-đọc-cả-hai-repo-khi-chuyển-sang-private).

| Dữ liệu | Cấu hình lưu trữ / khôi phục |
| --- | --- |
| Terraform state | Bucket có sẵn `manhnguyen1131`, key `retail/lab/terraform.tfstate`; versioning, mã hóa, S3 lockfile; không expiration |
| Prometheus | gp3 30Gi, retention 7 ngày / tối đa 24GB; request 500m / 2Gi, limit 1000m / 4Gi |
| Grafana / Alertmanager | gp3 5Gi / 2Gi; dashboard và cấu hình được khai báo trong Git |
| Loki / Alloy | Loki SingleBinary, PVC 10Gi và bucket `retail-lab-<account-id>-logs`, retention 7 ngày; Alloy DaemonSet thu log theo node |
| Kubernetes resource | Velero → `retail-lab-<account-id>-backup/velero/`, hằng ngày, giữ 14 ngày; loại trừ Secret do ESO tái tạo |
| MySQL | RDS automated backup / PITR 7 ngày |
| Redis | Snapshot hằng ngày, giữ 7 ngày |
| EFS | Chưa bật vì chưa có workload cần thư mục chia sẻ |

StorageClass `gp3-retain` mã hóa, `WaitForFirstConsumer`, cho phép tăng dung lượng, reclaim `Retain`. Volume được giữ lại sau xóa PVC vẫn cần quản lý và tính phí. Velero ở cấu hình này không snapshot EBS, không backup RDS/Redis, không thay thế quy trình khôi phục backend.

Alert đã có rule cho crash, thiếu replica, UI rollout lỗi, HTTP 5xx, PVC và backup. Kênh email/Telegram cần người vận hành cấu hình trong Secrets Manager; `alerting.enabled` mặc định false đến khi có receiver thật. RPO MySQL 15 phút / RTO 2 giờ là mục tiêu của bản thiết kế, phải đo bằng restore thực tế.

## Từ repo GitOps đến môi trường chạy thật

1. Trong repo source, làm theo [`scripts/infra/README.md`](https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop/blob/main/scripts/infra/README.md): chuẩn bị state bucket, AWS profile, IAM/OIDC và platform secrets. Điền tfvars từ `infra/retail-lab/terraform.tfvars.example` bằng IP/ARN thật.
2. Chạy `scripts/infra/resolve-addons.py`, khóa build add-on, chạy `scripts/infra/preflight.py --tfvars <file-json>`. Preflight kiểm tra identity/IAM/state, EKS/add-on, engine backend, RDS instance class, MQ cluster mode, EC2 offerings theo AZ và OpenSearch instance/storage/node limits. Redis kiểm tra engine metadata; node-type orderability mới chưa thể suy ra từ API đổi loại của cluster đã có. Kết quả metadata không bảo đảm capacity/quota hoặc chi phí; xác nhận bằng account và plan thật trước apply. Boundary role workload do admin quản lý phải khớp policy đã review ở source.
3. Tạo Terraform plan, đọc diff rồi apply plan đã duyệt theo workflow nguồn. AWS account, VPC ID, SG ID, ARN role, endpoint và bucket thực tế sẽ xuất ra từ Terraform.
4. Chạy backend bootstrap từ trong VPC bằng `scripts/infra/run_bootstrap.py` của repo source. Bước này tạo database/user, vhost, quyền search và giá trị Secrets Manager. Backend phải sẵn sàng trước khi bật app.
5. Dùng playbook `prepare-gitops.yml` trong repo source để tạo candidate riêng từ output/receipt Terraform; chỉ bật candidate sau backend bootstrap. Review candidate, chạy validator, tạo PR/merge GitOps rồi fetch canonical `main`. Không chạy import output trực tiếp trên checkout đang phục vụ bootstrap. CLI `scripts/prepare.py` vẫn là renderer canonical được playbook dùng; xem [runbook Ansible](https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop/blob/main/ansible/README.md).
6. Chạy `bootstrap-argocd.yml` trên controller Linux/WSL với AWS profile đúng account; script `scripts/bootstrap-argocd.ps1` vẫn là đường bootstrap operator thay thế. Kiểm tra sync platform, ESO, StorageClass và dev; thực hiện acceptance rồi mới promote đúng bộ digest sang prod.

Các thao tác rotation, restore drill và nghiệm thu monitoring/backup có playbook/helper trong repo source. Rotation dùng chung Lease với bootstrap, đồng bộ Secret trước khi tạo PR thay `secretRevision`; restore mặc định plan tài nguyên cô lập và không tự cutover. Xem [rotation](https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop/blob/main/docs/CREDENTIAL_ROTATION.md), [restore drill](https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop/blob/main/docs/RESTORE_DRILL.md), [operations acceptance](https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop/blob/main/docs/OPERATIONS_ACCEPTANCE.md). Code không thay thế bằng chứng chạy thật hoặc xác nhận nhận được alert.

Không cần người dùng tự đoán VPC ID/ARN để hoàn thiện repo ban đầu. Các đầu vào phải do người vận hành cung cấp trước khi tạo hạ tầng là account/profile/role quản trị, IP public quản trị, quyền trên state bucket và domain nếu muốn HTTPS. Token GitOps và receiver alert là cấu hình riêng, không phải metadata Terraform tự sinh.

Chi phí cần dự trù gồm EKS control plane, tối thiểu ba EC2 worker, ba broker RabbitMQ, RDS, Redis, OpenSearch, NAT, ALB, EBS, Secrets Manager và log/S3; nhiều thành phần vẫn tính phí khi không có người dùng. Cấu hình này ưu tiên giữ đúng kiến trúc lab và yêu cầu MQ HA. Chưa có ước tính tiền vì thời gian chạy, lưu lượng và giá tại tài khoản chưa được chốt; dùng [AWS Pricing Calculator](https://calculator.aws/) với các số lượng trên trước khi apply.

Nghiệm thu AWS gồm: pod đúng digest, checkout end-to-end, MySQL giữ dữ liệu sau restart, reindex/search, dev không truy cập prod, rotation secret, HPA/Autoscaler, preview/promote/abort, MQ failover và restore có đo thời gian. Helm render và CI thành công chưa chứng minh các điều kiện runtime này.
