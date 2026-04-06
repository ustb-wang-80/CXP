# 大恒相机高速采集与异步保存脚本说明

本项目核心脚本为 `new.py`，用于通过 `gxipy` 从大恒相机高速采集图像，并通过“采集线程 + 多进程保存”的架构实现实时预览与高吞吐落盘。

## 1. 功能概览

- 自动枚举并打开第一台可用相机。
- 启动前统一配置相机关键参数（触发模式、曝光、增益、帧率、镜像、ROI 偏移）。
- 自动识别 Bayer 排列并选择对应 OpenCV 去马赛克转换码。
- 采集线程使用 `dq_buf/q_buf` 零拷贝接口抓帧，尽可能缩短相机缓冲占用时间。
- 将耗时的颜色转换与 JPEG 压缩保存下放到多个子进程并行处理。
- 主线程负责实时预览与键盘退出控制（按 `q` 安全停止）。

## 2. 运行架构

脚本采用三层并发结构：

- 主线程
  - 负责相机初始化、启动/停止流程控制、预览窗口显示、退出信号管理。
- 采集线程（`acquire_thread`）
  - 专注“快速取图 -> 深拷贝 -> 立即归还缓冲”。
  - 把图像数据送入跨进程队列给保存工人。
  - 抽帧给预览队列，避免 UI 线程被高帧率压垮。
- 多个保存子进程（`save_process_worker`）
  - 负责 Bayer 转彩色（或黑白直存）与 JPEG 压缩写盘。
  - 通过“毒药丸”（`None`）实现优雅退出。

## 3. 关键流程说明

### 3.1 Bayer 识别与像素格式处理

函数：`resolve_bayer_cvt_code(cam)`

- 通过 `FeatureControl` 获取 `PixelFormat` 枚举节点。
- 尝试将彩色相机固定为 `BAYER_GB8`（若不支持则保留当前格式）。
- 优先依据 `PixelFormat` 识别 Bayer 排列，失败时回退 `PixelColorFilter`。
- 若检测到 `Mono`，返回 `(None, "MONO")`，后续跳过彩色转换。

### 3.2 相机初始化

函数：`init_camera_params(cam, target_fps)`

- 关闭触发，启用连续采集。
- 设置曝光时间与增益。
- 打开帧率控制并尝试锁定到目标帧率。
- 读取实际帧率并进行阈值校验（低于目标较多时拒绝启动）。
- 做安全兜底：
  - 强制关闭 `ReverseX/ReverseY`（避免镜像导致图像方向异常）。
  - 强制 `OffsetX/OffsetY = 0`（避免 Bayer 阵列错位）。

### 3.3 采集线程设计

函数：`acquire_thread(cam, mp_queue, display_queue, is_recording_event)`

- 设置采集缓冲数：`set_acquisition_buffer_number(30)`。
- `stream_on()` 后循环执行：
  1. 用 `dq_buf(timeout=1000)` 取帧。
  2. 丢弃坏帧（`INCOMPLETE`）并及时 `q_buf` 归还。
  3. 用 `get_numpy_array()` 得到图像后立刻 `.copy()` 深拷贝。
  4. 深拷贝数据进入 `mp_queue`，交由子进程保存。
  5. 每 3 帧抽 1 帧送预览队列。
  6. 采集线程内立即 `q_buf(raw_image)` 归还底层缓冲。
- 每 15 帧打印一次抓取速率与队列积压。

### 3.4 保存子进程设计

函数：`save_process_worker(mp_queue, worker_id, bayer_cvt_code)`

- 启动时确保输出目录 `./dataset_images` 存在。
- 从队列阻塞取任务：
  - 收到 `None` 立即退出。
  - 彩色模式：`cv2.cvtColor` 去马赛克后保存 JPEG。
  - 黑白模式：直接保存单通道图像。
- 文件命名：`frame_<capture_ts_ns>.jpg`。

### 3.5 主线程预览与退出

- 主线程从 `display_queue` 取预览帧。
- 彩色相机做去马赛克后显示；黑白相机直接显示。
- 按 `q` 后清除事件标志，触发全链路退出。

## 4. 关键参数（可按需调优）

在 `main()` 中：

- `TARGET_FPS = 15.0`：目标采集帧率。
- `NUM_WORKERS = 3`：JPEG 保存子进程数量（CPU 资源足够可提高）。
- `mp_queue = mp.Queue(maxsize=60)`：跨进程队列容量（缓冲 I/O 抖动）。
- `display_queue = queue.Queue(maxsize=1)`：预览只保留最新帧，避免 UI 堆积。

## 5. 输出结果

- 所有图片保存在 `./dataset_images/`。
- 格式为 JPEG，当前压缩质量设置为 `90`（速度与质量折中）。
- 文件名时间戳来自主机侧 `time.time_ns()`，适合顺序标识与粗粒度时序分析。

## 6. 依赖环境

- Python 3.x
- `gxipy`（大恒相机 SDK Python 接口）
- `opencv-python`
- 相机驱动与 SDK 已正确安装，且设备可被枚举

## 7. 运行方式

在项目根目录执行：

```bash
python new.py
```

启动后会弹出预览窗口，按 `q` 安全停止。

## 8. 已处理的稳定性问题（本版）

本次已对 `new.py` 做以下修正，提高健壮性：

- 修复黑白相机预览时仍做 Bayer 转换导致的报错风险。
- 修复多保存进程并发创建输出目录时的竞态问题。
- 采集线程异常路径补充 `q_buf` 归还，降低缓冲未归还风险。
- `mp_queue.qsize()` 在部分平台不可用时回退为 `N/A`，避免监控打印引发中断。

## 9. 后续可改进项（建议）

- 优先使用 `RawImage.get_timestamp()` 作为相机侧时间戳，并在不可用时回退主机时间。
- 将图片保存改为可选 PNG/无损格式，便于后续视觉算法训练。
- 增加配置文件（YAML/JSON）统一管理帧率、曝光、增益、保存路径等参数。
- 增加运行统计（丢帧率、平均写盘耗时、队列水位）用于性能压测。
