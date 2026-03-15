#!/usr/bin/env python3
"""
健康检查脚本 - 验证工具服务器的基本功能
"""

import json
import os
import subprocess
import sys
import time

def test_toolserver():
    """测试工具服务器的基本功能"""
    print("=== 工具服务器健康检查 ===")
    
    # 1. 检查工具服务器二进制文件
    repo_root = os.path.dirname(os.path.dirname(__file__))
    toolserver_path = os.path.join(repo_root, "toolserver")
    
    if not os.path.exists(toolserver_path):
        print(f"❌ 工具服务器二进制文件不存在: {toolserver_path}")
        return False
    
    print(f"✅ 工具服务器二进制文件存在: {toolserver_path}")
    
    # 2. 启动工具服务器进程
    env = dict(os.environ)
    env["TOOLSERVER_ROOT"] = repo_root
    
    try:
        proc = subprocess.Popen(
            [toolserver_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=repo_root,
            text=True
        )
        
        # 给进程一点启动时间
        time.sleep(0.5)
        
        # 3. 测试 list_tools 方法
        request = {
            "id": "health_check_1",
            "method": "list_tools",
            "params": {}
        }
        
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        
        # 读取响应
        line = proc.stdout.readline()
        if not line:
            print("❌ 工具服务器没有响应")
            return False
        
        try:
            response = json.loads(line.strip())
            if response.get("id") != "health_check_1":
                print(f"❌ 响应ID不匹配: {response.get('id')}")
                return False
            
            if response.get("error"):
                print(f"❌ 工具服务器返回错误: {response['error']}")
                return False
            
            tools = response.get("result", [])
            if not tools:
                print("❌ 没有可用的工具")
                return False
            
            print(f"✅ 工具服务器响应正常，找到 {len(tools)} 个工具")
            
            # 列出所有工具
            print("\n可用工具:")
            for tool in tools:
                name = tool.get("name", "未知")
                desc = tool.get("description", "")
                print(f"  - {name}: {desc}")
            
        except json.JSONDecodeError as e:
            print(f"❌ JSON解析错误: {e}")
            print(f"原始响应: {line}")
            return False
        
        # 4. 测试 ls 工具
        request = {
            "id": "health_check_2",
            "method": "call_tool",
            "params": {
                "name": "ls",
                "input": {
                    "path": "."
                }
            }
        }
        
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        
        line = proc.stdout.readline()
        if line:
            try:
                response = json.loads(line.strip())
                if response.get("id") == "health_check_2" and not response.get("error"):
                    print("✅ ls 工具工作正常")
                else:
                    print(f"⚠️ ls 工具返回警告: {response.get('error')}")
            except:
                print("⚠️ ls 工具响应格式异常")
        
        # 5. 测试 view 工具
        request = {
            "id": "health_check_3",
            "method": "call_tool",
            "params": {
                "name": "view",
                "input": {
                    "file_path": "README.md"
                }
            }
        }
        
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        
        line = proc.stdout.readline()
        if line:
            try:
                response = json.loads(line.strip())
                if response.get("id") == "health_check_3" and not response.get("error"):
                    print("✅ view 工具工作正常")
                else:
                    print(f"⚠️ view 工具返回警告: {response.get('error')}")
            except:
                print("⚠️ view 工具响应格式异常")
        
        # 关闭进程
        proc.terminate()
        proc.wait(timeout=2)
        
        print("\n=== 健康检查完成 ===")
        return True
        
    except Exception as e:
        print(f"❌ 健康检查过程中出现异常: {e}")
        return False

def check_python_dependencies():
    """检查Python依赖"""
    print("\n=== Python依赖检查 ===")
    
    required_modules = ["json", "os", "subprocess", "sys", "time"]
    
    for module in required_modules:
        try:
            __import__(module)
            print(f"✅ {module} 模块可用")
        except ImportError:
            print(f"❌ {module} 模块不可用")
            return False
    
    return True

def check_go_environment():
    """检查Go环境"""
    print("\n=== Go环境检查 ===")
    
    try:
        result = subprocess.run(["go", "version"], 
                              capture_output=True, 
                              text=True, 
                              check=True)
        print(f"✅ Go版本: {result.stdout.strip()}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Go命令执行失败: {e}")
        return False
    except FileNotFoundError:
        print("❌ Go未安装或不在PATH中")
        return False

def main():
    """主函数"""
    print("开始AUTO-MVP项目健康检查...\n")
    
    all_passed = True
    
    # 检查Go环境
    if not check_go_environment():
        all_passed = False
    
    # 检查Python依赖
    if not check_python_dependencies():
        all_passed = False
    
    # 测试工具服务器
    if not test_toolserver():
        all_passed = False
    
    print("\n" + "="*50)
    if all_passed:
        print("✅ 所有健康检查通过！项目状态良好。")
        return 0
    else:
        print("⚠️ 部分健康检查未通过，请检查相关问题。")
        return 1

if __name__ == "__main__":
    sys.exit(main())