import time
import hashlib
import json
import os

def long_task_demo():
    """
    执行约12秒的计算密集任务（循环计算哈希）
    每隔一段时间打印进度，并把最终结果写到 workdir/long_task_result.json
    """
    print("开始执行长时间计算任务...")
    
    total_duration = 12  # 总持续时间12秒
    update_interval = 2  # 每2秒更新一次进度
    iterations_per_second = 100000  # 每秒迭代次数
    
    start_time = time.time()
    end_time = start_time + total_duration
    
    # 准备存储最终结果
    final_result = {
        "task_start_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time)),
        "total_duration_seconds": total_duration,
        "hash_iterations": 0,
        "final_hash": "",
        "task_end_time": "",
        "progress_updates": []
    }
    
    current_time = start_time
    iteration_count = 0
    
    while current_time < end_time:
        # 计算每次更新需要执行的迭代次数
        iterations_to_run = int(iterations_per_second * update_interval)
        
        # 执行计算密集的哈希计算
        for _ in range(iterations_to_run):
            data = f"iteration_{iteration_count}".encode('utf-8')
            hash_obj = hashlib.sha256(data)
            iteration_count += 1
        
        current_time = time.time()
        elapsed_time = current_time - start_time
        progress_percent = (elapsed_time / total_duration) * 100
        
        # 打印进度
        progress_info = {
            "time_elapsed": round(elapsed_time, 2),
            "progress_percent": round(progress_percent, 2),
            "iterations_completed": iteration_count
        }
        
        print(f"进度: {progress_percent:.1f}% - 已耗时: {elapsed_time:.2f}秒 - 已完成迭代: {iteration_count}")
        final_result["progress_updates"].append(progress_info)
        
        # 等待到下一个更新时间点
        time.sleep(update_interval)
    
    # 执行最后的哈希计算以生成最终结果
    final_data = f"final_result_{iteration_count}".encode('utf-8')
    final_hash = hashlib.sha256(final_data).hexdigest()
    
    end_time = time.time()
    total_time = end_time - start_time
    
    final_result.update({
        "task_end_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(end_time)),
        "total_time_seconds": round(total_time, 2),
        "hash_iterations": iteration_count,
        "final_hash": final_hash
    })
    
    print(f"任务完成! 总耗时: {total_time:.2f}秒")
    print(f"最终哈希值: {final_hash}")
    
    # 将结果写入JSON文件
    result_file_path = os.path.join(os.getcwd(), "long_task_result.json")
    with open(result_file_path, 'w', encoding='utf-8') as f:
        json.dump(final_result, f, indent=2, ensure_ascii=False)
    
    print(f"结果已保存到: {result_file_path}")
    return final_result

if __name__ == "__main__":
    result = long_task_demo()
    print("\n任务执行总结:")
    print(f"- 总耗时: {result['total_time_seconds']}秒")
    print(f"- 完成迭代次数: {result['hash_iterations']:,}")
    print(f"- 最终哈希值: {result['final_hash']}")
    print(f"- 进度更新次数: {len(result['progress_updates'])}")