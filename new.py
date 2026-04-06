import gxipy as gx
import cv2
import sys
import threading
import queue
import time
import os
import multiprocessing as mp

PIXEL_FORMAT_TO_BAYER = {
    gx.GxPixelFormatEntry.BAYER_RG8: "RG",
    gx.GxPixelFormatEntry.BAYER_RG10: "RG",
    gx.GxPixelFormatEntry.BAYER_RG12: "RG",
    gx.GxPixelFormatEntry.BAYER_RG14: "RG",
    gx.GxPixelFormatEntry.BAYER_RG16: "RG",
    gx.GxPixelFormatEntry.BAYER_GB8: "GB",
    gx.GxPixelFormatEntry.BAYER_GB10: "GB",
    gx.GxPixelFormatEntry.BAYER_GB12: "GB",
    gx.GxPixelFormatEntry.BAYER_GB14: "GB",
    gx.GxPixelFormatEntry.BAYER_GB16: "GB",
    gx.GxPixelFormatEntry.BAYER_BG8: "BG",
    gx.GxPixelFormatEntry.BAYER_BG10: "BG",
    gx.GxPixelFormatEntry.BAYER_BG12: "BG",
    gx.GxPixelFormatEntry.BAYER_BG14: "BG",
    gx.GxPixelFormatEntry.BAYER_BG16: "BG",
    gx.GxPixelFormatEntry.BAYER_GR8: "GR",
    gx.GxPixelFormatEntry.BAYER_GR10: "GR",
    gx.GxPixelFormatEntry.BAYER_GR12: "GR",
    gx.GxPixelFormatEntry.BAYER_GR14: "GR",
    gx.GxPixelFormatEntry.BAYER_GR16: "GR",
}

COLOR_FILTER_TO_BAYER = {
    gx.GxPixelColorFilterEntry.BAYER_RG: "RG",
    gx.GxPixelColorFilterEntry.BAYER_GB: "GB",
    gx.GxPixelColorFilterEntry.BAYER_BG: "BG",
    gx.GxPixelColorFilterEntry.BAYER_GR: "GR",
}

BAYER_TO_CV2_BGR = {
    "RG": cv2.COLOR_BayerRG2BGR,
    "GB": cv2.COLOR_BayerGB2BGR,
    "BG": cv2.COLOR_BayerBG2BGR,
    "GR": cv2.COLOR_BayerGR2BGR,
}


def resolve_bayer_cvt_code(cam):
    """
    基于 gxipy 节点固定并识别 Bayer 格式，然后返回对应的 OpenCV 转换码。
    支持自动识别黑白/彩色相机。
    """
    # 推荐使用新版 API 获取属性控制器
    remote_feature = cam.get_remote_device_feature_control()
    pixel_format_feature = remote_feature.get_enum_feature("PixelFormat")

    # 尝试固定成 BAYER_GB8（仅彩色相机有效）
    try:
        if pixel_format_feature.is_implemented() and pixel_format_feature.is_writable():
            # 捕获异常以防黑白相机不支持该枚举
            pixel_format_feature.set(gx.GxPixelFormatEntry.BAYER_GB8)
            print("[像素格式] 已成功固定为 BAYER_GB8。")
    except Exception as e:
        print(f"[像素格式] 无法强制设置为 BAYER_GB8，将使用相机当前配置。")

    # 获取当前实际生效的像素格式
    # 注意：get() 返回的是 (int_value, string_value) 元组
    pf_val, pf_str = pixel_format_feature.get()
    print(f"[调试] 相机当前底层像素格式为: {pf_str} (0x{pf_val:X})")

    # 兼容处理：如果是黑白相机，不需要 Bayer 转换
    if "Mono" in pf_str:
        print("[硬件识别] 检测到该设备为黑白相机 (Mono)，跳过色彩转换流程。")
        return None, "MONO"

    # 提取正确的整数值去字典匹配
    bayer_tag = PIXEL_FORMAT_TO_BAYER.get(pf_val)

    # 备用方案：如果 PixelFormat 查不到，查 PixelColorFilter
    if bayer_tag is None:
        try:
            color_filter_feature = remote_feature.get_enum_feature("PixelColorFilter")
            cf_val, cf_str = color_filter_feature.get()
            bayer_tag = COLOR_FILTER_TO_BAYER.get(cf_val)
        except Exception:
            pass

    if bayer_tag is None:
        raise RuntimeError(f"无法识别的彩色排列格式，当前获取的格式为: {pf_str}")

    cvt_code = BAYER_TO_CV2_BGR[bayer_tag]
    print(f"[Bayer识别] 采用排列={bayer_tag}, OpenCV码={cvt_code}")
    
    return cvt_code, bayer_tag

def init_camera_params(cam, target_fps):
    """初始化相机参数"""
    print("\n--- 正在初始化相机底层参数 ---")
    cam.TriggerMode.set(gx.GxSwitchEntry.OFF)
    cam.AcquisitionMode.set(gx.GxAcquisitionModeEntry.CONTINUOUS)
    print("[1/4] 已关闭硬触发，强制设为连续自动采集模式。")

    cam.ExposureTime.set(50000.0)
    cam.Gain.set(0.0)
    print(f"[2/4] 曝光时间设为: {cam.ExposureTime.get()} us, 增益: {cam.Gain.get()} dB。")

    cam.AcquisitionFrameRateMode.set(gx.GxSwitchEntry.ON)
    cam.AcquisitionFrameRate.set(target_fps)
    print(f"[3/4] 已向相机发送帧率锁定指令，目标: {target_fps} FPS。")

    try:
        actual_fps = cam.CurrentAcquisitionFrameRate.get()
    except Exception:
        actual_fps = cam.AcquisitionFrameRate.get()

    print(f"[4/4] 硬件底层实际生效帧率反馈: {actual_fps:.2f} FPS")

    if actual_fps < (target_fps - 5):
        raise RuntimeError("相机实际帧率未达到预期目标，拒绝启动采集！")
    else:
        print("[自检通过] 相机帧率已完美锁定，符合预期！\n")
    print("\n--- 正在执行安全兜底初始化 ---")
    # 彻底封死原因二：强制关闭图像硬件镜像 [cite: 3232]
    try:
        if cam.ReverseX.is_implemented() and cam.ReverseX.is_writable():
            cam.ReverseX.set(False)
        if cam.ReverseY.is_implemented() and cam.ReverseY.is_writable():
            cam.ReverseY.set(False)
        print("[安全兜底] 硬件镜像已强制关闭。")
    except Exception as e:
        print(f"[安全兜底] 检查镜像参数时跳过: {e}")

    # 彻底封死原因三：强制 ROI 偏移量归零（偶数坐标） [cite: 3230]
    try:
        if cam.OffsetX.is_implemented() and cam.OffsetX.is_writable():
            cam.OffsetX.set(0)
        if cam.OffsetY.is_implemented() and cam.OffsetY.is_writable():
            cam.OffsetY.set(0)
        print("[安全兜底] ROI 偏移量已强制归零，确保 Bayer 阵列完整。")
    except Exception as e:
        print(f"[安全兜底] 检查 ROI 偏移量时跳过: {e}")


# ==========================================
# 优化：支持多工人的后台保存进程
# ==========================================
def save_process_worker(mp_queue, worker_id, bayer_cvt_code):
    """
    专门负责耗时的色彩转换和 JPEG 压缩保存
    worker_id: 用于区分是哪个进程在干活
    """
    if not os.path.exists('./dataset_images'):
        os.makedirs('./dataset_images')

    print(f"[保存工人 {worker_id} 号] 已启动，准备接单...")
    
    while True:
        try:
            # 阻塞等待获取数据
            item = mp_queue.get()
            
            # 收到“毒药丸”(None) 代表主程序要求退出
            if item is None:
                print(f"[保存工人 {worker_id} 号] 收到下班信号，正在安全退出...")
                break

            bayer_image, capture_ts_ns = item

            # --- 1. Bayer -> BGR ---
            # 注意：cv2.imwrite/cv2.imshow 默认按 BGR 解释三通道图像
            # --- 1. 图像转换 ---
            if bayer_cvt_code is not None:
                color_image = cv2.cvtColor(bayer_image, bayer_cvt_code)
            else:
                # 如果是黑白相机，不需要转换，直接保存单通道或转为三通道伪彩
                color_image = bayer_image

            # --- 2. 耗时操作：JPEG 压缩与保存 ---
            img_path = f"./dataset_images/frame_{capture_ts_ns}.jpg"
            # 适当降低一点点 JPEG 画质（95 -> 90）肉眼看不出区别，但能大幅降低 CPU 压缩负担
            cv2.imwrite(img_path, color_image, [int(cv2.IMWRITE_JPEG_QUALITY), 90])

        except Exception as e:
            print(f"[保存工人 {worker_id} 号 异常]: {e}")


def acquire_thread(cam, mp_queue, display_queue, is_recording_event):
    """取图线程：极致极速，只管拿数据、深拷贝、还内存"""
    cam.data_stream[0].set_acquisition_buffer_number(30)
    cam.stream_on()

    frame_count = 0
    start_time = time.time()

    while is_recording_event.is_set():
        try:
            raw_image = cam.data_stream[0].dq_buf(timeout=1000)
            # 坏帧不处理、缓冲必归还、流程不断流
            if raw_image is None or raw_image.get_status() == gx.GxFrameStatusList.INCOMPLETE:
                if raw_image is not None:
                    cam.data_stream[0].q_buf(raw_image)
                continue

            capture_ts_ns = time.time_ns()
            numpy_image = raw_image.get_numpy_array()

            if numpy_image is not None:
                # 【极其关键的一步】：必须深拷贝！
                # 因为马上就要调用 q_buf 把底层内存还给相机了，如果不拷贝，传给子进程的内存会被下一帧覆盖导致画面撕裂
                bayer_copy = numpy_image.copy()

                # 送去跨进程队列
                try:
                    mp_queue.put_nowait((bayer_copy, capture_ts_ns))
                except queue.Full:
                    print("[警告] 队列已满！系统算力已达极限，丢弃当前帧。")
                    pass

                # 抽帧送去预览 (每 3 帧抽 1 帧)
                if frame_count % 3 == 0:
                    if not display_queue.full():
                        preview_bayer = cv2.resize(bayer_copy, (1024, 800))
                        display_queue.put(preview_bayer)

            frame_count += 1
            
            # 打印采集监控（放在主线程打印最准确）
            if frame_count % 15 == 0:
                elapsed = time.time() - start_time
                real_fps = 15 / elapsed
                print(f"[主控台] 成功抓取: {frame_count} 帧 | 抓取速率: {real_fps:.2f} FPS | 待处理积压: {mp_queue.qsize()}")
                start_time = time.time()

            # 终极底线：立刻归还零拷贝内存
            cam.data_stream[0].q_buf(raw_image)

        except Exception as e:
            if "timeout" in str(e).lower():
                continue
            print(f"[取图异常]: {e}")
            is_recording_event.clear()
            break


def main():
    TARGET_FPS = 15.0
    # 开启几个后台进程？建议设置为 3 或 4。如果你的电脑 CPU 核心很多，可以设为 4。
    NUM_WORKERS = 3 

    # 1. 初始化跨进程队列和线程队列
    # mp_queue 容量设为 60，用来吸收系统 I/O 波动
    mp_queue = mp.Queue(maxsize=60)
    display_queue = queue.Queue(maxsize=1)
    is_recording_event = threading.Event()
    is_recording_event.set()

    workers = []
    bayer_cvt_code = None

    # 3. 初始化相机
    device_manager = gx.DeviceManager()
    dev_num, dev_info_list = device_manager.update_all_device_list()
    if dev_num == 0:
        print("未检测到相机设备，请检查连线。")
        sys.exit(1)

    cam = device_manager.open_device_by_sn(dev_info_list[0].get("sn"))
    t_acquire = None

    try:
        init_camera_params(cam, TARGET_FPS)
        bayer_cvt_code, bayer_tag = resolve_bayer_cvt_code(cam)

        # ==========================================
        # 核心改动：启动多个后台保存进程
        # ==========================================
        for i in range(NUM_WORKERS):
            p = mp.Process(target=save_process_worker, args=(mp_queue, i + 1, bayer_cvt_code))
            p.start()
            workers.append(p)
        
        # 4. 启动取图线程
        t_acquire = threading.Thread(target=acquire_thread, args=(cam, mp_queue, display_queue, is_recording_event))
        t_acquire.start()

        print("=========================================================")
        print(f"采集已启动！已分配 {NUM_WORKERS} 个 CPU 核心进行后台加速处理。")
        print(f"当前 Bayer 排列已固定/识别为: {bayer_tag}")
        print("在弹出的图像窗口中按下键盘 'q' 键，即可安全停止录制。")
        print("=========================================================")

        # 主线程负责 UI 显示
        while is_recording_event.is_set():
            try:
                # 获取的是 Bayer 格式的缩小图
                preview_bayer = display_queue.get(timeout=0.1)
                
                # 在主线程中将其转为彩色用于显示
                preview_color = cv2.cvtColor(preview_bayer, bayer_cvt_code)
                cv2.imshow('Camera Live Monitor', preview_color)
                
            except queue.Empty:
                pass

            if cv2.waitKey(10) & 0xFF == ord('q'):
                print("\n接收到停止指令，正在通知底层安全停止...")
                is_recording_event.clear()
                break

    except Exception as e:
        print(f"\n主流程异常：{e}")
    finally:
        is_recording_event.clear()
        cv2.destroyAllWindows()

        if t_acquire is not None:
            t_acquire.join()

        try:
            cam.stream_off()
            cam.close_device()
        except Exception:
            pass

        # 5. 优雅地关闭子进程
        print("正在等待后台保存进程清空队列...")
        # 有几个工人，就要发几个“毒药丸”，让它们各自下班
        for _ in range(NUM_WORKERS):
            mp_queue.put(None)
            
        for p in workers:
            p.join()

        print("底层资源已全部释放，程序完美退出。")

if __name__ == '__main__':
    # Windows 下多进程必须要有 freeze_support
    mp.freeze_support()
    main()