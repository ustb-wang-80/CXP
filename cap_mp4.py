import gxipy as gx
import cv2
import sys
import threading
import queue
import time
import os

is_recording = True

# 存图专用队列（优先实时性，容量 30；满时会丢帧避免阻塞采集）
image_queue = queue.Queue(maxsize=30)
# 显示专用队列（容量必须为 1，满了直接丢弃，绝不阻塞后台存图）
display_queue = queue.Queue(maxsize=1)


def init_camera_params(cam, target_fps):
    """
    初始化相机参数，包含强制状态重置与帧率校验
    """
    print("\n--- 正在初始化相机底层参数 ---")

    # ==========================================
    # 1. 强制重置采集模式 (新增优化)
    # ==========================================
    # 显式关闭外部硬触发模式，防止相机卡死等待信号
    cam.TriggerMode.set(gx.GxSwitchEntry.OFF)
    # 显式设置为连续采集模式 (Continuous)
    cam.AcquisitionMode.set(gx.GxAcquisitionModeEntry.CONTINUOUS)
    print("[1/4] 已关闭硬触发，强制设为连续自动采集模式。")

    # ==========================================
    # 2. 设置曝光与增益
    # ==========================================
    # 注意：10000 us = 10 ms。此时相机的物理极限帧率最高约为 100 FPS
    cam.ExposureTime.set(10000.0)
    cam.Gain.set(0.0)
    print(f"[2/4] 曝光时间设为: {cam.ExposureTime.get()} us, 增益: {cam.Gain.get()} dB。")

    # ==========================================
    # 3. 设置并锁定目标帧率
    # ==========================================
    cam.AcquisitionFrameRateMode.set(gx.GxSwitchEntry.ON)
    cam.AcquisitionFrameRate.set(target_fps)
    print(f"[3/4] 已向相机发送帧率锁定指令，目标: {target_fps} FPS。")

    # ==========================================
    # 4. 严格校验实际生效帧率 (新增优化)
    # ==========================================
    # 大恒相机有一个 CurrentAcquisitionFrameRate 节点，
    # 它会真实反映在当前曝光时间和带宽限制下，相机真正能跑到的上限。
    try:
        actual_fps = cam.CurrentAcquisitionFrameRate.get()
    except Exception:
        # 兼容部分老型号
        actual_fps = cam.AcquisitionFrameRate.get()

    print(f"[4/4] 硬件底层实际生效帧率反馈: {actual_fps:.2f} FPS")

    # 允许 5 FPS 的浮点数计算误差
    if actual_fps < (target_fps - 5):
        print("\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
        print(f"[致命警告] 相机无法达到你要求的 {target_fps} FPS！")
        print(f"当前物理极限被卡在了 {actual_fps:.2f} FPS。")
        print("原因排查：\n 1. 曝光时间太长 (当前限制了最高帧率)\n 2. 采集卡带宽被限制\n 3. 开启了耗时的图像预处理功能")
        print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n")
        # 工业代码标准：如果不达标，直接抛出异常阻止程序继续运行，防止采集到残次数据
        raise RuntimeError("相机实际帧率未达到预期目标，拒绝启动采集！")
    else:
        print("[自检通过] 相机帧率已完美锁定，符合预期！\n")


def acquire_thread(cam):
    """取图线程：极致极速，只管零拷贝拿数据"""
    global is_recording

    # 分配 30 个底层 DMA 缓冲池
    cam.data_stream[0].set_acquisition_buffer_number(30)
    cam.stream_on()

    while is_recording:
        try:
            raw_image = cam.data_stream[0].dq_buf(timeout=1000)
            if raw_image is None or raw_image.get_status() == gx.GxFrameStatusList.INCOMPLETE:
                if raw_image is not None:
                    cam.data_stream[0].q_buf(raw_image)
                continue

            # 使用电脑时间戳（纳秒）作为采集时刻，随帧传入存图线程
            capture_ts_ns = time.time_ns()
            if not image_queue.full():
                image_queue.put((raw_image, capture_ts_ns))
            else:
                cam.data_stream[0].q_buf(raw_image)
        except Exception as e:
            # 超时属于常规现象；其他异常需要可见并触发安全停机
            if "timeout" in str(e).lower():
                continue
            print(f"[取图异常]: {e}")
            is_recording = False
            break


def record_thread(cam):
    """存图线程：稳定保存 JPEG 序列，顺手提供预览帧"""
    global is_recording

    if not os.path.exists('dataset_images'):
        os.makedirs('dataset_images')

    frame_count = 0
    start_time = time.time()

    while is_recording or not image_queue.empty():
        try:
            item = image_queue.get(timeout=1.0)
            raw_image = None
            try:
                if isinstance(item, tuple) and len(item) == 2:
                    raw_image, capture_ts_ns = item
                else:
                    # 兼容旧结构：若队列元素不是二元组则回退到当前时间
                    raw_image = item
                    capture_ts_ns = time.time_ns()

                numpy_image = raw_image.get_numpy_array()
                if numpy_image is not None:
                    # --- 1. 核心任务：安全、高质量保存图片 ---
                    img_path = f"dataset_images/frame_{capture_ts_ns}.jpg"
                    cv2.imwrite(img_path, numpy_image, [int(cv2.IMWRITE_JPEG_QUALITY), 95])

                    # --- 2. 附加任务：抽帧送去预览 (每 3 帧抽 1 帧) ---
                    if frame_count % 3 == 0:
                        if display_queue.empty():
                            preview_img = cv2.resize(numpy_image, (1024, 800))
                            display_queue.put(preview_img)

                frame_count += 1

                # --- 3. 打印存图进度与真实耗时监控 ---
                if frame_count % 15 == 0:
                    elapsed = time.time() - start_time
                    real_save_fps = 15 / elapsed
                    print(
                        f"[存图监控] 已保存: {frame_count} 帧 | 实际存图速率: {real_save_fps:.2f} FPS | 队列积压: {image_queue.qsize()}")
                    start_time = time.time()
            finally:
                # 终极底线：无论中间是否异常，都必须归还零拷贝内存
                if raw_image is not None:
                    cam.data_stream[0].q_buf(raw_image)
                image_queue.task_done()

        except queue.Empty:
            continue
        except Exception as e:
            print(f"[存图异常]: {e}")


def main():
    global is_recording

    # 设定你想锁定的稳定帧率 (建议先用 15 进行跑通测试)
    TARGET_FPS = 15.0

    device_manager = gx.DeviceManager()
    dev_num, dev_info_list = device_manager.update_all_device_list()
    if dev_num == 0:
        print("未检测到相机设备，请检查连线。")
        sys.exit(1)

    cam = device_manager.open_device_by_sn(dev_info_list[0].get("sn"))
    t_record = None
    t_acquire = None

    try:
        # 执行初始化与状态自检
        init_camera_params(cam, TARGET_FPS)
        # 启动后台工作线程
        t_record = threading.Thread(target=record_thread, args=(cam,))
        t_record.start()

        t_acquire = threading.Thread(target=acquire_thread, args=(cam,))
        t_acquire.start()

        print("=========================================================")
        print("采集已启动！正在显示实时监控预览...")
        print("在弹出的图像窗口中按下键盘 'q' 键，即可安全停止录制。")
        print("=========================================================")

        # 主线程负责 UI 显示
        while is_recording:
            try:
                preview_frame = display_queue.get(timeout=0.1)

                # 彩色与黑白兼容处理
                if len(preview_frame.shape) == 2:
                    preview_frame = cv2.cvtColor(preview_frame, cv2.COLOR_GRAY2BGR)

                cv2.imshow('Camera Live Monitor', preview_frame)
            except queue.Empty:
                pass

            if cv2.waitKey(10) & 0xFF == ord('q'):
                print("\n接收到停止指令，正在通知底层安全停止...")
                is_recording = False
                break
    except RuntimeError as e:
        print(e)
    except KeyboardInterrupt:
        print("\n检测到 Ctrl+C，正在安全停止...")
    except Exception as e:
        print(f"\n主流程异常：{e}")
    finally:
        is_recording = False
        cv2.destroyAllWindows()

        if t_acquire is not None:
            t_acquire.join()
        if t_record is not None:
            t_record.join()

        try:
            cam.stream_off()
        except Exception:
            pass
        try:
            cam.close_device()
        except Exception:
            pass

        print("底层资源已全部释放，程序完美退出。")


if __name__ == '__main__':
    main()