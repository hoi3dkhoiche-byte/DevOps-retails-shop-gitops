# Chọn cấu hình lab hoặc architecture

`deploymentProfile` trong `config.yaml` chọn footprint Kubernetes; `deployment.enabled` vẫn là công tắc kích hoạt riêng. Repo mặc định **architecture, chưa kích hoạt**: bật đầy đủ platform và cấu hình cả dev/prod theo thiết kế. `lab` vẫn có thể chọn tường minh để giảm footprint cho vòng tạo môi trường → kiểm tra → xóa tài nguyên. Hai profile không thay đổi năm digest đã kiểm thử hay `release.backend_profile: mysql-redis-opensearch`.

| Thành phần | `lab` | `architecture` |
| --- | --- | --- |
| Worker dự kiến từ Terraform | Một management và một dev, mỗi node 2 vCPU/8GiB; không production | Management 4 vCPU/16GiB, dev/prod theo thiết kế đầy đủ |
| Argo CD, ESO, LB Controller, Rollouts, metrics-server | Giữ, một replica cần thiết | Giữ |
| Dex, Notifications | Tắt | `enabled: true`; SSO và kênh thông báo cần cấu hình thật |
| ApplicationSet controller | `replicas: 0`; chart đã pin không có công tắc `enabled` | `replicas: 1` |
| Prometheus/Grafana/PodMonitor CRD | Giữ để kiểm tra metrics ứng dụng | Giữ |
| Prometheus | 6 giờ, tối đa 512MB, `emptyDir` giới hạn 2GiB | 7 ngày, tối đa 24GB, PVC 30GiB |
| Grafana | Không PVC; không datasource Loki/CloudWatch bổ sung | PVC và datasource theo thiết kế |
| Alertmanager | Tắt; không nghiệm thu gửi thông báo | Giữ; receiver cần cấu hình riêng |
| Loki, Alloy, Velero, cluster-autoscaler | Không tạo Application | Giữ đầy đủ |
| Production configuration/workloads | Không tạo configuration Application; chart từ chối release production được bật | Luồng promote riêng sau nghiệm thu dev |

Các [values chung](../platform/) có CPU/memory requests và limits cho controller, sidecar, init container và hook Job. [Overlays lab](../profiles/lab/) chỉ giảm footprint, không chứa account/ARN hoặc image release. Application con nạp values chung rồi overlay; bootstrap Ansible/PowerShell cũng nạp đúng overlay cho ESO và Argo CD. Chọn lại architecture không giữ sót giá trị lab trong file nền.

`architecture` tạo configuration Application cho cả dev và prod khi môi trường được kích hoạt. Bộ image production vẫn chờ luồng nghiệm thu/promote dev; không tự lấy image chưa nghiệm thu để bật workload prod. `alerting.enabled` chỉ bật cấu hình gửi alert đến receiver bên ngoài, không tắt Alertmanager của profile đầy đủ. Repository credential vẫn chỉ bật nếu repo chuyển private. Velero giữ chế độ sao lưu Kubernetes vào S3; snapshot EBS/node-agent không thuộc thiết kế đã chốt. Loki giữ chế độ SingleBinary, nên các replica backend/read/write của chế độ distributed vẫn bằng 0.

## Hợp đồng đầu ra Terraform

Terraform mới xuất `deployment_profile` kiểu string, giá trị `lab` hoặc `architecture`. Exporter source cho phép metadata không sensitive này; `prepare.py --terraform-output FILE` nhập sang `deploymentProfile`. Output cũ không có trường đó dùng **architecture** để giữ hành vi trước đây. Giá trị khác hoặc profile khác giữa Terraform và GitOps sẽ chặn bootstrap Ansible.

Chọn profile ở Terraform trước plan rồi review toàn bộ candidate GitOps. Không đổi profile để chữa lỗi thiếu capacity trên cluster đang chạy: root không tự prune, nên Application optional cũ có thể vẫn tồn tại. Helper từ chối đổi profile của cấu hình đang bật; dọn môi trường cũ trước, chuẩn bị lại cấu hình disabled, rồi mới nhập output và kích hoạt môi trường mới. `deployment.enabled: false` không tự xóa cluster hoặc workload cũ.

## Bằng chứng footprint

Từ repo GitOps, có Python/PyYAML và Helm đã pin:

```bash
python scripts/check-platforms.py --download --cache .local/platform-charts --report .local/platform-budget.json
```

Lệnh tải đúng chart version/appVersion trong `versions.yaml`, render cả hai profile và kiểm tra resources/placement thật. Prometheus/Alertmanager là CR do operator tạo StatefulSet, nên gate tính cả container chính và config-reloader từ flags operator được render; không bỏ qua hai workload này. Gate tính replica, DaemonSet trên management, init container/native sidecar, toàn bộ hook Job và thêm một pod lớn nhất để chừa chỗ cập nhật.

Lab phải giữ tổng requests cộng pod thay thế trong **1400m CPU/4GiB RAM**, tổng memory limits cộng pod thay thế trong **6GiB** và tối đa 21 platform pod kể cả thay thế. Như vậy còn ít nhất 600m CPU/2GiB cho hệ điều hành, EKS add-on và system pod trên node 2 vCPU/8GiB. Bộ chart hiện render 17 platform pod kể cả hook, requests 880m/1968MiB và limits RAM 4384MiB trước pod thay thế. Đây là bằng chứng cấu hình/scheduling offline, không phải số RAM/CPU đã đo khi chạy thật hoặc bảo đảm ứng dụng chịu tải.

Architecture cũng có gate cho node management ban đầu 4 vCPU/16Gi: requests cộng một Pod thay thế tối đa 3000m CPU/12Gi RAM, tối đa 32 platform Pod kể cả thay thế; dành 1 vCPU/4Gi cho OS và EKS add-on. Memory limits của bộ đầy đủ có thể oversubscribe; report ghi rõ trạng thái này, không đánh đồng limit với request khi tính scheduling. Phải đo áp lực bộ nhớ thực tế khi nghiệm thu AWS.

Workflow GitOps chạy gate này trên PR/push và giữ report/log 30 ngày. Không gọi AWS/Kubernetes và không tạo cluster trong validation. Lab không có log tập trung, Velero hoặc lưu metrics lâu dài; nghiệm thu backup/restore, alert delivery và production cần architecture cùng phép thử vận hành thật.
