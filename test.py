import time
import requests
from guacamole.client import GuacamoleClient

# Guacamole伺服器地址
server_ip = '0.0.0.0'
guacamole_url = f"http://{server_ip}:8080/guacamole"

# 登錄並獲取認證令牌
auth_data = {
    "username": "guacadmin",
    "password": "guacadmin"
}

# 第一步：獲取認證令牌
try:
    auth_response = requests.post(
        f"{guacamole_url}/api/tokens",
        data=auth_data
    )
    auth_response.raise_for_status()
    auth_token = auth_response.json().get("authToken")
    print(f"獲取到認證令牌: {auth_token}")
except Exception as e:
    print(f"認證失敗: {e}")
    exit(1)

# 第二步：獲取連接列表並找到"New"連接
try:
    connections_response = requests.get(
        f"{guacamole_url}/api/session/data/mysql/connections",
        params={"token": auth_token}
    )
    connections_response.raise_for_status()
    connections = connections_response.json()
    
    connection_id = None
    for conn_id, conn_info in connections.items():
        if conn_info["name"] == "New":
            connection_id = conn_id
            print(f"找到'New'連接，ID: {connection_id}")
            break
            
    if connection_id is None:
        print("未找到名為'New'的連接ID")
        exit(1)
except Exception as e:
    print(f"獲取連接列表失敗: {e}")
    exit(1)

# 第三步：使用pyguacamole連接guacd並控制RDP會話
try:
    # 連接到guacd伺服器
    client = GuacamoleClient(server_ip, 4822)
    
    # 與guacd進行握手，使用找到的連接ID
    client.handshake(protocol='rdp', connection_id=connection_id)
    print("成功與guacd握手")
    
    # 接收來自guacd的指令
    instruction = client.receive()
    print(f"接收到指令: {instruction}")

    # 使用try-except包裝每個send操作
    try:
        client.send('mouse:0.500,500,0;')
        print("已發送鼠標移動指令")
        # 添加確認接收
        instruction = client.receive()
        print(f"指令確認: {instruction}")
    except Exception as e:
        print(f"發送指令時出錯: {e}")

    
    # 模擬鍵盤輸入 (輸入"Hello")
    for char in "Hello":
        key_code = ord(char)
        client.send(f'key:1.{key_code},0;')  # 按鍵按下
        client.send(f'key:0.{key_code},0;')  # 按鍵釋放
        time.sleep(0.1)
    print("已發送鍵盤輸入指令")
    
    # 繼續接收一段時間的指令
    print("開始接收指令...")
    for i in range(10):
        try:
            instruction = client.receive()
            print(f"指令 {i+1}: {instruction}")
        except Exception as e:
            print(f"接收指令時出錯: {e}")
        time.sleep(0.5)
    
    # 關閉連接
    client.close()
    print("已關閉連接")
except Exception as e:
    print(f"連接guacd或控制RDP會話失敗: {e}")
