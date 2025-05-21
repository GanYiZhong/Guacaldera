import os
import json
import shutil
import socket
import asyncio
import logging
import aiohttp
import docker
import base64
import requests
import time
import uuid
import threading
import select
from base64 import b64encode
from urllib.parse import urljoin
from aiohttp import web
from aiohttp_jinja2 import template

import sys
sys.path.append(os.path.dirname(os.path.realpath(__file__)))

name = 'Guacamole'
description = '通過 Apache Guacamole 提供遠程桌面會話管理功能'
address = '/plugin/guacamole/gui'
docker_client = None
guacd_container = None
guacamole_container = None
mysql_container = None
plugin_root = None
session_manager = None

# Guacamole 配置
GUAC_URL = "http://localhost:8080/guacamole/"
GUACD_HOST = "localhost"
GUACD_PORT = 4822
DATA_SOURCE = "mysql"

class GuacamoleAutomator:
    def __init__(self):
        self.token = None
        self.client = None
        self.connected = False
        self.heartbeat_active = False
        self.instruction_poster_func = None
        self.active_streams = {}
        self.recorded_commands = []
        self.is_recording = False
        self.connection_id = None
        self.last_activity = time.time()  # 添加最後活動時間追蹤

    def generate_client_url(self, connection_id):
        connection_str = f"{connection_id}\0c\0{DATA_SOURCE}"
        client_hash = b64encode(connection_str.encode()).decode().strip('=')
        return urljoin(GUAC_URL, f"#/client/{client_hash}")

    def authenticate(self, username, password):
        auth_url = urljoin(GUAC_URL, "api/tokens")
        response = requests.post(
            auth_url,
            data={'username': username, 'password': password},
            headers={'Content-Type': 'application/x-www-form-urlencoded'}
        )
        response.raise_for_status()
        self.token = response.json()['authToken']
        logging.info("認證成功")

    def get_connections(self):
        url = urljoin(GUAC_URL, f"api/session/data/{DATA_SOURCE}/connections")
        response = requests.get(url, params={'token': self.token})
        response.raise_for_status()
        return response.json()

    def get_connection_details(self, connection_id):
        try:
            self.connection_id = connection_id
            info_url = urljoin(GUAC_URL, f"api/session/data/{DATA_SOURCE}/connections/{connection_id}")
            info_resp = requests.get(info_url, params={'token': self.token})
            info_resp.raise_for_status()
            protocol = info_resp.json().get('protocol', 'rdp')
            
            param_url = urljoin(GUAC_URL, f"api/session/data/{DATA_SOURCE}/connections/{connection_id}/parameters")
            param_resp = requests.get(param_url, params={'token': self.token})
            param_resp.raise_for_status()
            
            return {
                'protocol': protocol,
                'parameters': param_resp.json() or {}  # 確保返回空字典而不是None
            }
        except Exception as e:
            logging.error(f"獲取連接詳情失敗: {str(e)}")
            # 返回默認值，確保結構完整
            return {
                'protocol': 'rdp',
                'parameters': {}
            }

    def connect_guacd(self, connection_details):
        try:
            self.client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.client.settimeout(15)
            self.client.connect((GUACD_HOST, GUACD_PORT))
            self.connected = True
            logging.info(f"已連接到guacd {GUACD_HOST}:{GUACD_PORT}")

            self._handshake(connection_details)

            self.heartbeat_active = True
            threading.Thread(target=self._heartbeat, name="GuacHeartbeat", daemon=True).start()
            threading.Thread(target=self.message_receive_loop, name="GuacReceiver", daemon=True).start()
            return True
        except Exception as e:
            self.connected = False
            logging.error(f"連接失敗: {str(e)}")
            self.close()
            return False

    def _handshake(self, details):
        self.client.setblocking(True)
        self._send('select', details.get('protocol', 'rdp'))
        opcode, server_params = self._receive_blocking_for_handshake()
        if opcode != 'args':
            raise ValueError(f"協議錯誤，預期'args'收到'{opcode}' 但收到參數 '{server_params}'")

        self._send_size()
        self._send_audio()
        self._send_video()
        self._send_image()
        self._send_timezone()

        connect_args = ["VERSION_1_5_0"]
        parameters = details.get('parameters', {})
        
        for param in server_params[1:]:
            value = parameters.get(param, "")
            connect_args.append(value if value else "")

        encoded_args = []
        for arg in connect_args:
            if arg == "":
                encoded_args.append("0.")
            else:
                encoded_args.append(f"{len(str(arg))}.{arg}")

        connect_instr = f"{len('connect')}.connect,{','.join(encoded_args)};"
        self.client.sendall(connect_instr.encode('utf-8'))
        logging.debug(f"發送 → {connect_instr}")

        opcode, ready_params = self._receive_blocking_for_handshake()
        if opcode != 'ready':
            raise ValueError(f"握手失敗，收到'{opcode}' params '{ready_params}'")
        logging.info(f"協議握手完成！客戶端ID: {ready_params[0]}")

        if self.instruction_poster_func:
            self._safe_post_instruction('size', (0, 1024, 768))
            self._safe_post_instruction('sync', (int(time.time() * 1000),))

    def _send_size(self): self._send('size', '1024', '768', '96')
    def _send_audio(self): self._send('audio')
    def _send_video(self): self._send('video')
    def _send_image(self): self._send('image', 'image/png', 'image/jpeg')
    def _send_timezone(self): self._send('timezone', 'Asia/Shanghai')

    def _send(self, opcode, *args_tuple):
        if not self.connected: raise ConnectionError("連接未就緒")
        instr = self._encode_instruction(opcode, *args_tuple)
        try:
            self.client.sendall(instr.encode('utf-8'))
            logging.debug(f"發送 → {instr}")
            self.last_activity = time.time()  # 更新最後活動時間
        except Exception as e:
            logging.error(f"發送指令 '{opcode}' 失敗: {e}")
            self.connected = False
        time.sleep(0.01)

    def _encode_instruction(self, opcode: str, *args_tuple) -> str:
        elements = [f"{len(str(opcode))}.{opcode}"]
        for arg_val in args_tuple:
            arg_str = str(arg_val)
            if arg_str == "":
                elements.append("0.")
            else:
                elements.append(f"{len(arg_str)}.{arg_str}")
        return ','.join(elements) + ';'

    def _receive_blocking_for_handshake(self) -> tuple:
        buffer = bytearray()
        self.client.setblocking(True)
        try:
            while True:
                chunk = self.client.recv(4096)
                if not chunk:
                    logging.warning("Socket 連接在 _receive_blocking_for_handshake 中斷開")
                    self.connected = False
                    return ("", ())
                buffer.extend(chunk)
                if b';' in buffer:
                    end_index = buffer.find(b';') + 1
                    data_bytes = buffer[:end_index]
                    return self._parse_instruction(data_bytes.decode('utf-8', errors='replace').strip())
        except socket.timeout:
            logging.error("Socket 在 _receive_blocking_for_handshake 中超時 (握手階段不應發生)。")
            self.connected = False
            return ("", ())
        except Exception as e:
            logging.error(f"_receive_blocking_for_handshake 出錯: {e}")
            self.connected = False
            return ("", ())

    def _parse_instruction(self, data: str) -> tuple:
        if not data or not data.endswith(';'):
            return ("", ())

        parts = []
        buffer_str = data[:-1]
        while buffer_str:
            dot_pos = buffer_str.find('.')
            if dot_pos == -1:
                break
            try:
                length = int(buffer_str[:dot_pos])
            except ValueError:
                break
            start = dot_pos + 1
            end = start + length
            if end > len(buffer_str):
                break

            parts.append(buffer_str[start:end])
            buffer_str = buffer_str[end:]

            if buffer_str.startswith(','):
                buffer_str = buffer_str[1:]
            elif buffer_str:
                break

        return (parts[0], tuple(parts[1:])) if parts else ("", ())

    def _heartbeat(self):
        last_ping_time = 0
        ping_interval = 5  # 5秒發送一次ping
        ping_timeout = 20  # 將超時時間從10秒增加到20秒
        
        while self.heartbeat_active and self.connected:
            try:
                current_time = time.time()
                
                # 檢查是否需要發送ping
                if current_time - last_ping_time >= ping_interval:
                    self._send('ping', str(int(current_time * 1000)))
                    last_ping_time = current_time
                
                # 檢查是否超時，但增加容忍度
                if current_time - self.last_activity > ping_timeout:
                    logging.warning(f"心跳超時: 最後活動時間 {self.last_activity}, 當前時間 {current_time}")
                    # 嘗試再發送一次ping，而不是立即斷開
                    self._send('ping', str(int(current_time * 1000)))
                    # 給一點額外時間等待響應
                    time.sleep(2)
                    # 如果仍然沒有活動，才斷開連接
                    if current_time - self.last_activity > ping_timeout:
                        self.connected = False
                        break
                
                # 短暫休眠，避免CPU使用率過高
                time.sleep(0.5)
                
            except ConnectionError:
                logging.warning("心跳失敗: 發送時連接丟失。")
                # 嘗試等待一下再重試，而不是立即斷開
                time.sleep(1)
                continue
            except Exception as e:
                logging.error(f"心跳線程出錯: {str(e)}")
                # 嘗試等待一下再重試，而不是立即斷開
                time.sleep(1)
                continue

    def _safe_post_instruction(self, opcode, args):
        """安全地發送指令到前端，避免異步問題"""
        try:
            if callable(self.instruction_poster_func):
                self.instruction_poster_func(opcode, args)
            self.last_activity = time.time()  # 更新最後活動時間
        except Exception as e:
            logging.error(f"發送指令到前端失敗: {type(e).__name__} - {str(e)}")

    def send_key(self, keysym, pressed):
        if not self.connected: raise ConnectionError("連接已中斷")
    
    # 如果是字符鍵，使用特殊處理
        if isinstance(keysym, int) and 32 <= keysym <= 126:  # 可打印ASCII範圍
        # 對於可打印字符，使用Guacamole的Unicode模式
            self._send('key', str(keysym), '1' if pressed else '0')
        elif isinstance(keysym, str) and keysym.startswith('0x'):
        # 處理十六進制格式的keysym
            keysym = int(keysym, 16)
            self._send('key', str(keysym), '1' if pressed else '0')
        else:
        # 對於特殊鍵，直接發送keysym
            self._send('key', str(keysym), '1' if pressed else '0')
    
        if self.is_recording:
            self.recorded_commands.append(f"key {keysym} {'1' if pressed else '0'}")


    def send_mouse(self, x, y, button_mask):
        if not self.connected: raise ConnectionError("連接已中斷")
        self._send('mouse', str(x), str(y), str(button_mask))
        if self.is_recording:
            self.recorded_commands.append(f"mouse {x} {y} {button_mask}")

    def type_text(self, text):
        if not self.connected: raise ConnectionError("連接已中斷")
        for char in text:
            keysym = ord(char)
            self.send_key(keysym, True)
            time.sleep(0.05)
            self.send_key(keysym, False)
            time.sleep(0.05)
        if self.is_recording:
            self.recorded_commands.append(f"type {text}")

    def start_recording(self):
        self.is_recording = True
        self.recorded_commands = []
        logging.info("開始錄製命令")
        return True

    def stop_recording(self):
        self.is_recording = False
        logging.info(f"停止錄製，共記錄了 {len(self.recorded_commands)} 條命令")
        return self.recorded_commands

    def execute_script(self, script_content):
        if not self.connected: raise ConnectionError("連接已中斷")
        commands = script_content.strip().split('\n')
        for cmd in commands:
            cmd = cmd.strip()
            if not cmd or cmd.startswith('#'):
                continue
            parts = cmd.split()
            if not parts:
                continue
            try:
                if parts[0] == 'mouse':
                    if len(parts) >= 4:
                        x, y, button = int(parts[1]), int(parts[2]), int(parts[3])
                        action = parts[4] if len(parts) > 4 else "move"
                        if action == "click":
                            self.send_mouse(x, y, button)
                            time.sleep(0.1)
                            self.send_mouse(x, y, 0)
                        elif action == "down":
                            self.send_mouse(x, y, button)
                        elif action == "up":
                            self.send_mouse(x, y, 0)
                        else:
                            self.send_mouse(x, y, 0)
                elif parts[0] == 'key':
                    i = 1
                    keys_to_press = []
                    while i < len(parts):
                        if parts[i] == 'key' and i+1 < len(parts):
                            i += 1
                            key = parts[i]
                            if key.isdigit():
                                keys_to_press.append(int(key))
                            else:
                                special_keys = {
                                    'enter': 0xFF0D, 'tab': 0xFF09, 'escape': 0xFF1B,
                                    'space': 0x0020, 'backspace': 0xFF08, 'left': 0xFF51,
                                    'up': 0xFF52, 'right': 0xFF53, 'down': 0xFF54,
                                    'ctrl': 0xFFE3, 'alt': 0xFFE9, 'shift': 0xFFE1,
                                    'win': 0xFFEB, 'a': 0x0061, 'b': 0x0062, 'c': 0x0063,
                                    'return': 0xFF0D
                                }
                                keys_to_press.append(special_keys.get(key.lower(), ord(key[0])))
                        else:
                            key = parts[i]
                            if key.isdigit():
                                keys_to_press.append(int(key))
                            else:
                                special_keys = {
                                    'enter': 0xFF0D, 'tab': 0xFF09, 'escape': 0xFF1B,
                                    'space': 0x0020, 'backspace': 0xFF08, 'left': 0xFF51,
                                    'up': 0xFF52, 'right': 0xFF53, 'down': 0xFF54,
                                    'ctrl': 0xFFE3, 'alt': 0xFFE9, 'shift': 0xFFE1,
                                    'win': 0xFFEB, 'a': 0x0061, 'b': 0x0062, 'c': 0x0063,
                                    'return': 0xFF0D
                                }
                                keys_to_press.append(special_keys.get(key.lower(), ord(key[0])))
                        i += 1
                    for key in keys_to_press:
                        self.send_key(key, True)
                        time.sleep(0.05)
                    for key in reversed(keys_to_press):
                        self.send_key(key, False)
                        time.sleep(0.05)
                elif parts[0] == 'type':
                    text = ' '.join(parts[1:])
                    self.type_text(text)
                elif parts[0] == 'wait':
                    if len(parts) > 1:
                        wait_time = float(parts[1])
                        time.sleep(wait_time)
                elif parts[0] == 'script':
                    if len(parts) > 1:
                        script_name = parts[1]
                        if script_name == 'open_cmd':
                            self.execute_script("""
key win
key r
wait 0.5
type cmd
key enter
""")
                        elif script_name == 'open_notepad':
                            self.execute_script("""
key win
key r
wait 0.5
type notepad
key enter
""")
                        elif script_name == 'take_screenshot':
                            self.execute_script("""
key win
key printscreen
""")
                time.sleep(0.1)
            except Exception as e:
                logging.error(f"執行腳本命令 '{cmd}' 時出錯: {e}")
        return True

    def close(self):
        if self.client:
            logging.info(f"正在關閉 Guacamole 連接 (socket fd: {self.client.fileno() if self.client else 'N/A'})")
            self.heartbeat_active = False
            if self.connected:
                try:
                    disconnect_instr = self._encode_instruction('disconnect')
                    self.client.sendall(disconnect_instr.encode('utf-8'))
                    logging.info("發送 disconnect 指令給 guacd")
                except Exception as e:
                    logging.warning(f"發送 disconnect 指令失敗 (socket 可能已關閉): {e}")
            self.connected = False
            try:
                self.client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.client.close()
            logging.info("Socket 已成功關閉")
        self.client = None

    def message_receive_loop(self):
        logging.info("啟動 Guacamole 消息接收循環...")
        instruction_buffer = ""
        if self.client:
            self.client.settimeout(0.5)  # 增加超時時間
        
        connection_errors = 0
        max_errors = 3  # 允許的最大連續錯誤數
        
        while self.connected:
            try:
                # 使用select進行非阻塞讀取
                readable, _, _ = select.select([self.client], [], [], 0.5)
                if not readable:
                    continue
                
                raw_data_chunk = self.client.recv(16384)
                if not raw_data_chunk:
                    logging.info("Guacd 連接已斷開 (recv 返回空數據)。")
                    
                    # 嘗試短暫等待後再重試，而不是立即斷開
                    time.sleep(1)
                    connection_errors += 1
                    if connection_errors >= max_errors:
                        self.connected = False
                        break
                    continue
                
                # 成功接收數據，重置錯誤計數
                connection_errors = 0
                
                instruction_buffer += raw_data_chunk.decode('utf-8', errors='replace')
                self.last_activity = time.time()  # 更新最後活動時間

                while ';' in instruction_buffer:
                    instr_end_idx = instruction_buffer.find(';') + 1
                    full_instruction = instruction_buffer[:instr_end_idx]
                    instruction_buffer = instruction_buffer[instr_end_idx:]

                    opcode, params = self._parse_instruction(full_instruction)

                    if opcode:
                        if opcode == 'error':
                            logging.error(f"收到 Guacd 錯誤: {params}")
                            if self.instruction_poster_func:
                                self._safe_post_instruction(opcode, params)
                            if params and "UPSTREAM_ERROR" in params[0].upper() and "closed" in params[0].lower():
                                logging.warning("上游 RDP/VNC 伺服器關閉了連接。")
                                self.connected = False
                                break
                            continue

                        if opcode == 'img' or opcode == 'file':
                            if len(params) >= 1:
                                stream_index = params[0]
                                self.active_streams[stream_index] = {
                                    'type': opcode,
                                    'params': params,
                                    'data': []
                                }
                        elif opcode == 'blob':
                            if len(params) >= 1:
                                stream_index = params[0]
                                if stream_index in self.active_streams:
                                    if len(params) >= 2:
                                        self.active_streams[stream_index]['data'].append(params[1])
                        elif opcode == 'end':
                            if len(params) >= 1:
                                stream_index = params[0]
                                if stream_index in self.active_streams:
                                    del self.active_streams[stream_index]
                        elif opcode == 'pong':
                            # 處理pong響應，更新最後活動時間
                            self.last_activity = time.time()
                            logging.debug("收到 pong 響應")

                        if self.instruction_poster_func:
                            self._safe_post_instruction(opcode, params)

                        if opcode == 'disconnect':
                            logging.info("收到 Guacd 的 disconnect 指令。")
                            self.connected = False
                            break
            except socket.timeout:
                # 超時不是錯誤，繼續循環
                continue
            except UnicodeDecodeError as e:
                logging.error(f"解碼 Guacd 數據失敗: {e}. Buffer: {repr(instruction_buffer[:100])}")
                instruction_buffer = ""
            except ConnectionResetError:
                logging.warning("Guacd 連接被遠程重置。")
                self.connected = False
                break
            except BrokenPipeError:
                logging.warning("Guacd 連接 BrokenPipeError。")
                self.connected = False
                break
            except Exception as e:
                if self.connected:
                    logging.error(f"消息接收循環發生未知錯誤: {type(e).__name__} - {str(e)}")
                self.connected = False
                break

        logging.info("Guacamole 消息接收循環已停止。")
        if self.client:
            self.client.settimeout(None)
        self.close()


class GuacamoleController:
    def __init__(self):
       self.automator = GuacamoleAutomator()
       self.instance_id = str(uuid.uuid4())[:8]  # 添加唯一標識符
    
       # 獲取基礎 logger
       base_logger = logging.getLogger('guacamole_controller')
    
       # 設置基礎 logger 的處理器和格式化器
       if not base_logger.handlers:  # 避免重複添加處理器
           handler = logging.StreamHandler()
           formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
           handler.setFormatter(formatter)
           base_logger.addHandler(handler)
           base_logger.setLevel(logging.INFO)
    
       # 使用 LoggerAdapter 添加 instance_id 上下文
       self.logger = logging.LoggerAdapter(base_logger, {'instance_id': self.instance_id})
    
       self.connection_id = None
       self.logger.info(f"GuacamoleController 初始化成功 [ID: {self.instance_id}]")

    
    async def connect(self, connection_id, token=None):
        """連接到指定的連接ID"""
        try:
            self.connection_id = connection_id
            
            # 如果沒有提供token，則進行身份驗證
            if not token:
                self.automator.authenticate('guacadmin', 'guacadmin')
                token = self.automator.token
            else:
                self.automator.token = token
            
            # 獲取連接詳情
            connection_details = self.automator.get_connection_details(connection_id)
            
            # 確保connection_details包含必要的結構
            if 'protocol' not in connection_details or 'parameters' not in connection_details:
                self.logger.error(f"連接詳情格式不正確: {connection_details}")
                return False
            
            # 連接到 guacd
            if self.automator.connect_guacd(connection_details):
                self.logger.info(f"成功連接到 {connection_id}")
                return True
            else:
                self.logger.error(f"連接失敗: {connection_id}")
                return False
        except Exception as e:
            self.logger.error(f"連接失敗: {str(e)}")
            return False
    
    async def disconnect(self):
        """斷開連接"""
        try:
            if self.automator:
                self.automator.close()
                self.logger.info("已斷開連接")
                return True
            return True
        except Exception as e:
            self.logger.error(f"斷開連接時出錯: {str(e)}")
            return False
    
    async def mouse_event(self, x, y, button, action):
        """發送鼠標事件"""
        try:
            button_mask = 0
            if button == 1:  # 左鍵
                button_mask |= 1
            elif button == 2:  # 中鍵
                button_mask |= 2
            elif button == 3:  # 右鍵
                button_mask |= 4
            
            if action == "click":
                self.automator.send_mouse(x, y, button_mask)
                await asyncio.sleep(0.05)
                self.automator.send_mouse(x, y, 0)
            elif action == "down":
                self.automator.send_mouse(x, y, button_mask)
            elif action == "up":
                self.automator.send_mouse(x, y, 0)
            elif action == "move":
                self.automator.send_mouse(x, y, 0)
            
            self.logger.info(f"鼠標事件: x={x}, y={y}, 按鈕={button}, 動作={action}")
            return True
        except Exception as e:
            self.logger.error(f"發送鼠標事件時出錯: {str(e)}")
            return False
    
    async def key_event(self, key, state=None):
        """發送鍵盤事件"""
        try:
            key_map = {
                'return': 0xFF0D, 'enter': 0xFF0D,
                'tab': 0xFF09,
                'escape': 0xFF1B, 'esc': 0xFF1B,
                'ctrl': 0xFFE3,
                'alt': 0xFFE9,
                'shift': 0xFFE1,
                'win': 0xFFEB,
                'up': 0xFF52,
                'down': 0xFF54,
                'left': 0xFF51,
                'right': 0xFF53,
                'space': 0x0020,
                'delete': 0xFFFF, 'del': 0xFFFF,
                'backspace': 0xFF08,
                
                # 字母鍵 - 使用小寫ASCII值
                'a': 0x0061, 'b': 0x0062, 'c': 0x0063, 'd': 0x0064,
                'e': 0x0065, 'f': 0x0066, 'g': 0x0067, 'h': 0x0068,
                'i': 0x0069, 'j': 0x006A, 'k': 0x006B, 'l': 0x006C,
                'm': 0x006D, 'n': 0x006E, 'o': 0x006F, 'p': 0x0070,
                'q': 0x0071, 'r': 0x0072, 's': 0x0073, 't': 0x0074,
                'u': 0x0075, 'v': 0x0076, 'w': 0x0077, 'x': 0x0078,
                'y': 0x0079, 'z': 0x007A,
            
                # 數字鍵
                '0': 0x0030, '1': 0x0031, '2': 0x0032, '3': 0x0033,
                '4': 0x0034, '5': 0x0035, '6': 0x0036, '7': 0x0037,
                '8': 0x0038, '9': 0x0039
            }
            
            # 解析命令字符串
            parts = key.split()
            
            # 檢查是否是複合按鍵命令
            if len(parts) > 1:
                keys_to_press = []
                i = 0
                while i < len(parts):
                    if parts[i] == 'key' and i+1 < len(parts):
                        i += 1
                        current_key = parts[i]
                        if current_key.isdigit():
                            keys_to_press.append(int(current_key))
                        else:
                            keys_to_press.append(key_map.get(current_key.lower(), ord(current_key[0]) if current_key else 0))
                    else:
                        current_key = parts[i]
                        if current_key.isdigit():
                            keys_to_press.append(int(current_key))
                        else:
                            keys_to_press.append(key_map.get(current_key.lower(), ord(current_key[0]) if current_key else 0))
                    i += 1
                
                # 按下所有按鍵
                for k in keys_to_press:
                    self.automator.send_key(k, True)
                    await asyncio.sleep(0.05)
                
                # 釋放所有按鍵（按相反順序）
                for k in reversed(keys_to_press):
                    self.automator.send_key(k, False)
                    await asyncio.sleep(0.05)
                
                self.logger.info(f"複合按鍵: {key}")
                return True
            else:
                # 單個按鍵處理
                single_key = key
                if single_key.lower() in key_map:
                    keysym = key_map[single_key.lower()]
                elif len(single_key) == 1:
                    keysym = ord(single_key)
                else:
                    keysym = ord(single_key[0]) if single_key else 0
                
                # 確保鍵盤狀態正確傳遞
                state_bool = True if state in [True, 1, '1', 'true', 'True'] else False
                self.automator.send_key(keysym, state_bool)
                self.logger.info(f"按鍵: {single_key} (狀態: {state_bool})")
                return True
        except Exception as e:
            self.logger.error(f"發送鍵盤事件時出錯: {str(e)}")
            return False
    
    async def type_text(self, text):
        """輸入文本"""
        try:
            self.automator.type_text(text)
            self.logger.info(f"輸入文本: {text}")
            return True
        except Exception as e:
            self.logger.error(f"輸入文本時出錯: {str(e)}")
            return False
    
    async def execute_command(self, command):
        """執行單個命令"""
        try:
            cmd_parts = command.strip().split(' ')
            if not cmd_parts:
                self.logger.error("空命令")
                return False
            
            cmd_type = cmd_parts[0].lower()
            
            # 鼠標操作
            if cmd_type == 'mouse':
                if len(cmd_parts) < 4:
                    self.logger.error("無效的鼠標命令格式")
                    return False
                
                x = int(cmd_parts[1])
                y = int(cmd_parts[2])
                button = int(cmd_parts[3])
                action = cmd_parts[4] if len(cmd_parts) > 4 else "move"
                
                return await self.mouse_event(x, y, button, action)
            
            # 鍵盤操作
            elif cmd_type == 'key':
                if len(cmd_parts) < 2:
                    self.logger.error("無效的鍵盤命令格式")
                    return False
                
                key = cmd_parts[1]
                # 修正問題2：檢查是否有狀態參數並正確處理
                state = True
                if len(cmd_parts) > 2:
                    state = cmd_parts[2] == '1'
                
                return await self.key_event(key, state)
            
            # 輸入文本
            elif cmd_type == 'type':
                if len(cmd_parts) < 2:
                    self.logger.error("無效的輸入命令格式")
                    return False
                
                text = ' '.join(cmd_parts[1:])
                return await self.type_text(text)
            
            # 等待
            elif cmd_type == 'wait':
                if len(cmd_parts) < 2:
                    self.logger.error("無效的等待命令格式")
                    return False
                
                seconds = float(cmd_parts[1])
                await asyncio.sleep(seconds)
                self.logger.info(f"等待 {seconds} 秒")
                return True
            
            # 預定義腳本
            elif cmd_type == 'script':
                if len(cmd_parts) < 2:
                    self.logger.error("腳本名稱必須提供")
                    return False
                
                script_name = cmd_parts[1].lower()
                
                # 修正問題3：改進預定義腳本處理
                if script_name == 'open_cmd':
                    await self.execute_command('key win 1')
                    await asyncio.sleep(0.1)
                    await self.execute_command('key win 0')
                    await asyncio.sleep(0.2)
                    await self.execute_command('key r 1')
                    await asyncio.sleep(0.1)
                    await self.execute_command('key r 0')
                    await asyncio.sleep(0.5)
                    await self.execute_command('type cmd')
                    await asyncio.sleep(0.2)
                    await self.execute_command('key return 1')
                    await asyncio.sleep(0.1)
                    await self.execute_command('key return 0')
                    return True
                elif script_name == 'screenshot':
                    await self.execute_command('key win 1')
                    await asyncio.sleep(0.1)
                    await self.execute_command('key shift 1')
                    await asyncio.sleep(0.1)
                    await self.execute_command('key s 1')
                    await asyncio.sleep(0.1)
                    await self.execute_command('key s 0')
                    await asyncio.sleep(0.1)
                    await self.execute_command('key shift 0')
                    await asyncio.sleep(0.1)
                    await self.execute_command('key win 0')
                    return True
                elif script_name == 'browser':
                    await self.execute_command('key win 1')
                    await asyncio.sleep(0.1)
                    await self.execute_command('key win 0')
                    await asyncio.sleep(0.2)
                    await self.execute_command('key r 1')
                    await asyncio.sleep(0.1)
                    await self.execute_command('key r 0')
                    await asyncio.sleep(0.5)
                    await self.execute_command('type chrome')
                    await asyncio.sleep(0.2)
                    await self.execute_command('key return 1')
                    await asyncio.sleep(0.1)
                    await self.execute_command('key return 0')
                    return True
                elif script_name == 'notepad':
                    await self.execute_command('key win 1')
                    await asyncio.sleep(0.1)
                    await self.execute_command('key win 0')
                    await asyncio.sleep(0.2)
                    await self.execute_command('key r 1')
                    await asyncio.sleep(0.1)
                    await self.execute_command('key r 0')
                    await asyncio.sleep(0.5)
                    await self.execute_command('type notepad')
                    await asyncio.sleep(0.2)
                    await self.execute_command('key return 1')
                    await asyncio.sleep(0.1)
                    await self.execute_command('key return 0')
                    return True
                elif script_name == 'explorer':
                    await self.execute_command('key win 1')
                    await asyncio.sleep(0.1)
                    await self.execute_command('key e 1')
                    await asyncio.sleep(0.1)
                    await self.execute_command('key e 0')
                    await asyncio.sleep(0.1)
                    await self.execute_command('key win 0')
                    return True
                else:
                    # 如果不是預定義腳本，嘗試執行自定義腳本
                    return self.automator.execute_script(f"""
# 執行 {script_name} 腳本
script {script_name}
""")
            
            else:
                self.logger.error(f"未知的命令類型: {cmd_type}")
                return False
            
        except Exception as e:
            self.logger.error(f"執行命令時出錯: {str(e)}")
            return False
    
  #  async def execute_script(self, script):
   #     """執行多行腳本"""
   #     try:
    #        script_lines = [line.strip() for line in script.split('\n') if line.strip()]
     #       results = []
            
    #        for line in script_lines:
     #           result = await self.execute_command(line)
      #          results.append({
       #             'command': line,
        #            'success': result
         #       })
            
     #       self.logger.info(f"腳本執行完成，共 {len(script_lines)} 條命令")
      #      return results
      #  except Exception as e:
       #     self.logger.error(f"執行腳本時出錯: {str(e)}")
        #    return False

class GuacamoleSessionManager:
    def __init__(self):
        self.active_sessions = {}
        self.last_activity = {}
        self.lock = asyncio.Lock()
        self.script_registry = {
            'open_cmd': [
                ('key', 'win'), ('key', 'r'), ('wait', 0.5),
                ('type', 'cmd'), ('key', 'return')
            ],
            'screenshot': [
                ('key', 'win'), ('key', 'shift'), ('key', 's')
            ],
            'browser': [
                ('key', 'win'), ('key', 'r'), ('wait', 0.5),
                ('type', 'chrome'), ('key', 'return')
            ],
            'notepad': [
                ('key', 'win'), ('key', 'r'), ('wait', 0.5),
                ('type', 'notepad'), ('key', 'return')
            ],
            'explorer': [
                ('key', 'win'), ('key', 'e')
            ]
        }
        self.ws_connections = {}
        self.cleanup_task = None
        self.connection_semaphores = {}  # 為每個連接ID創建一個信號量
        
        self.start_cleanup_task()
        
    def start_cleanup_task(self):
        async def cleanup_inactive_sessions():
            while True:
                await asyncio.sleep(60)  # 每分鐘檢查一次
                await self.cleanup_inactive_sessions(timeout=300)  # 5分鐘無活動則清理
        
        if not self.cleanup_task:
            self.cleanup_task = asyncio.create_task(cleanup_inactive_sessions())
    
    async def cleanup_inactive_sessions(self, timeout=300):
        """清理不活躍的會話"""
        current_time = time.time()
        to_close = []
        
        async with self.lock:
            for conn_id, last_time in list(self.last_activity.items()):
                if current_time - last_time > timeout:
                    to_close.append(conn_id)
        
        for conn_id in to_close:
            logging.info(f"清理不活躍會話: {conn_id}")
            await self.close_session(conn_id)
    
    
    async def get_or_create_session(self, connection_id, token):
        """獲取或創建與特定連接的會話"""
        async with self.lock:
            if connection_id in self.active_sessions:
                self.last_activity[connection_id] = time.time()
                return self.active_sessions[connection_id]
            
            controller = GuacamoleController()
            
            if await controller.connect(connection_id, token):
                self.active_sessions[connection_id] = controller
                self.last_activity[connection_id] = time.time()
                return controller
            
            return None
    
    async def execute_command(self, connection_id, command, token):
        """在指定連接上執行命令"""
        # 獲取或創建此連接的信號量
        if connection_id not in self.connection_semaphores:
            self.connection_semaphores[connection_id] = asyncio.Semaphore(1)
        
        # 使用信號量限制並發
        async with self.connection_semaphores[connection_id]:
            try:
                client = await self.get_or_create_session(connection_id, token)
                if client:
                    result = await client.execute_command(command)
                    self.last_activity[connection_id] = time.time()  # 更新最後活動時間
                    return {'status': 'success', 'result': f"Command executed: {command}"}
                else:
                    return {'status': 'error', 'message': '無法獲取控制器'}
            except Exception as e:
                logging.error(f"Error executing command: {e}")
                return {'status': 'error', 'message': str(e)}
    
    async def execute_script(self, connection_id, script, token, ws=None):
        """執行多行腳本"""
        # 獲取或創建此連接的信號量
        if connection_id not in self.connection_semaphores:
            self.connection_semaphores[connection_id] = asyncio.Semaphore(1)
        
        # 使用信號量限制並發
        async with self.connection_semaphores[connection_id]:
            script_lines = [line.strip() for line in script.split('\n') if line.strip()]
            results = []
            
            try:
                if token is None:
                    token_response = await get_guacamole_token(None)
                    toekn_data = json.loads(token_response.text)
                    token = token_data.get('token')
                    
                client = await self.get_or_create_session(connection_id, token)
                if not client:
                    error_output = {
                        'line_number': 0,
                        'command': 'script',
                        'status': 'error',
                        'result': "無法獲取控制器"
                    }
                    results.append(error_output)
                    if ws:
                        await ws.send_json(error_output)
                    return results
                
                for idx, line in enumerate(script_lines):
                    try:
                        result = await client.execute_command(line)
                        output = {
                            'line_number': idx + 1,
                            'command': line,
                            'status': 'success' if result else 'error',
                            'result': f"Command executed: {line}" if result else "Command failed"
                        }
                        results.append(output)
                        
                        if ws:
                            await ws.send_json(output)
                        
                        if line.startswith('wait '):
                            try:
                                seconds = float(line.split()[1])
                                await asyncio.sleep(seconds)
                            except Exception as e:
                                logging.error(f"Invalid wait command: {str(e)}")
                    except Exception as e:
                        error_output = {
                            'line_number': idx + 1,
                            'command': line,
                            'status': 'error',
                            'result': str(e)
                        }
                        results.append(error_output)
                        if ws:
                            await ws.send_json(error_output)
                
                self.last_activity[connection_id] = time.time()  # 更新最後活動時間
            
            except Exception as e:
                error_output = {
                    'line_number': 0,
                    'command': 'script',
                    'status': 'error',
                    'result': f"Failed to execute script: {str(e)}"
                }
                results.append(error_output)
                if ws:
                    await ws.send_json(error_output)
            
            return results
    
    async def close_session(self, connection_id):
        """關閉指定的會話"""
        async with self.lock:
            if connection_id in self.active_sessions:
                try:
                    # 先關閉WebSocket連接
                    if connection_id in self.ws_connections:
                        ws = self.ws_connections[connection_id]
                        if not ws.closed:
                            await ws.close()
                        del self.ws_connections[connection_id]
                    
                    # 然後關閉控制器
                    await self.active_sessions[connection_id].disconnect()
                except Exception as e:
                    logging.error(f"Error closing session: {e}")
                finally:
                    del self.active_sessions[connection_id]
                    if connection_id in self.last_activity:
                        del self.last_activity[connection_id]
                    if connection_id in self.connection_semaphores:
                        del self.connection_semaphores[connection_id]
    
    def register_websocket(self, connection_id, ws):
        """註冊WebSocket連接到特定連接ID"""
        self.ws_connections[connection_id] = ws
    
    def unregister_websocket(self, connection_id):
        """取消註冊WebSocket連接"""
        if connection_id in self.ws_connections:
            del self.ws_connections[connection_id]
    
    async def broadcast_to_connection(self, connection_id, message):
        """向特定連接的所有WebSocket客戶端廣播消息"""
        if connection_id in self.ws_connections:
            ws = self.ws_connections[connection_id]
            if not ws.closed:
                await ws.send_json(message)
    
    async def _get_connection_details(self, connection_id, token):
        """從Guacamole API獲取連接詳情"""
        url = f"http://localhost:8080/guacamole/api/session/data/mysql/connections/{connection_id}?token={token}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise Exception(f"Failed to get connection details: {response.status}")
                    
                    data = await response.json()
                    parameters = data.get('parameters', {}) or {}  # 確保不是None
                    parameters.setdefault('hostname', '')
                    parameters.setdefault('port', '3389')
                    parameters.setdefault('username', '')
                    parameters.setdefault('password', '')
                    
                    return {
                        'protocol': data.get('protocol', 'rdp').lower(),
                        'parameters': parameters
                    }
        except Exception as e:
            logging.error(f"Connection detail error: {e}")
            return {
                'protocol': 'rdp',
                'parameters': {
                    'hostname': '',
                    'port': '3389',
                    'username': '',
                    'password': ''
                }
            }



async def enable(services):
    global docker_client, plugin_root, session_manager
    app = services.get('app_svc').application
    plugin_root = os.path.dirname(os.path.realpath(__file__))
    
    app.router.add_static('/plugin/guacamole/static', f'{plugin_root}/static', append_version=True)
    app.router.add_static('/plugin/guacamole/jquery', f'{plugin_root}/static/jquery', append_version=True)
    
    app.router.add_route('GET', '/plugin/guacamole/gui', gui)
    app.router.add_route('POST', '/plugin/guacamole/start', start_containers)
    app.router.add_route('POST', '/plugin/guacamole/stop', stop_containers)
    app.router.add_route('GET', '/plugin/guacamole/status', get_status)
    app.router.add_route('POST', '/plugin/guacamole/create_connection', create_connection)
    app.router.add_route('GET', '/plugin/guacamole/list_connections', list_connections)
    app.router.add_route('POST', '/plugin/guacamole/execute_command', execute_command)
    app.router.add_route('POST', '/plugin/guacamole/execute_script', execute_script)
    app.router.add_route('GET', '/plugin/guacamole/get_token', get_guacamole_token)
    app.router.add_route('GET', '/plugin/guacamole/scripts', get_scripts)
    app.router.add_route('GET', '/plugin/guacamole/ws', websocket_handler)
    app.router.add_route('GET', '/plugin/guacamole/display', display_handler)
    
    session_manager = GuacamoleSessionManager()
    asyncio.create_task(periodic_cleanup())
    try:
        docker_client = docker.from_env()
        logging.info("Docker client initialized successfully")
    except Exception as e:
        logging.error(f"Failed to initialize Docker client: {e}")

async def periodic_cleanup():
    """定期清理不活躍的會話"""
    while True:
        await asyncio.sleep(60)  # 每分鐘檢查一次
        try:
            await session_manager.cleanup_inactive_sessions()
        except Exception as e:
            logging.error(f"清理會話時出錯: {e}")

@template('guacamole.html')
async def gui(request):
    return {'name': 'Guacamole 插件', 'status': await get_container_status()}

async def get_container_status():
    status = {'guacd': 'stopped', 'guacamole': 'stopped', 'mysql': 'stopped'}
    if docker_client:
        try:
            containers = docker_client.containers.list(all=True)
            for container in containers:
                if 'guacd' in container.name:
                    status['guacd'] = container.status
                elif 'guacamole' in container.name and 'mysql' not in container.name:
                    status['guacamole'] = container.status
                elif 'mysql' in container.name:
                    status['mysql'] = container.status
        except Exception as e:
            logging.error(f"Error getting container status: {e}")
    return status

async def start_containers(request):
    global guacd_container, guacamole_container, mysql_container
    if not docker_client:
        return web.json_response({'status': 'error', 'message': 'Docker client not initialized'})
    
    try:
        network_name = 'guacamole_network'
        networks = docker_client.networks.list(names=[network_name])
        network = networks[0] if networks else docker_client.networks.create(network_name)
        
        mysql_container = start_mysql_container(network)
        await asyncio.sleep(15)
        guacd_container = start_guacd_container(network)
        guacamole_container = start_guacamole_container(network)
        
        return web.json_response({'status': 'success', 'message': 'Containers started successfully'})
    except PermissionError as e:
        logging.error(f"Permission error starting containers: {e}")
        return web.json_response({'status': 'error', 'message': '權限錯誤：無法啟動 Docker 容器。請確保 Caldera 有適當的 Docker 權限。'})
    except Exception as e:
        logging.error(f"Error starting containers: {e}")
        return web.json_response({'status': 'error', 'message': str(e)})

def start_mysql_container(network):
    try:
        mysql_containers = docker_client.containers.list(all=True, filters={'name': 'guacamole-mysql'})
        if mysql_containers:
            container = mysql_containers[0]
            if container.status != 'running':
                container.start()
            return container
        
        init_db_path = f'{plugin_root}/docker/init'
        os.makedirs(init_db_path, exist_ok=True)
        
        sql_url = "https://raw.githubusercontent.com/apache/guacamole-client/1.5.5/extensions/guacamole-auth-jdbc/modules/guacamole-auth-jdbc-mysql/schema/create-schema.sql"
        response = requests.get(sql_url)
        with open(f'{init_db_path}/initdb.sql', 'wb') as f:
            f.write(response.content)
        
        container = docker_client.containers.run(
            'mysql:8.0',
            name='guacamole-mysql',
            detach=True,
            environment={
                'MYSQL_ROOT_PASSWORD': 'guacamole_pass',
                'MYSQL_DATABASE': 'guacamole_db',
                'MYSQL_USER': 'guacamole_user',
                'MYSQL_PASSWORD': 'guacamole_pass'
            },
            volumes={init_db_path: {'bind': '/docker-entrypoint-initdb.d', 'mode': 'ro'}}
        )
        network.connect(container)
        return container
    except Exception as e:
        logging.error(f"Error starting MySQL container: {e}")
        raise

def start_guacd_container(network):
    try:
        guacd_containers = docker_client.containers.list(all=True, filters={'name': 'guacd'})
        if guacd_containers:
            container = guacd_containers[0]
            if container.status != 'running':
                container.start()
            return container
        
        container = docker_client.containers.run(
            'guacamole/guacd:1.5.5',
            name='guacd',
            detach=True
        )
        network.connect(container)
        return container
    except Exception as e:
        logging.error(f"Error starting guacd container: {e}")
        raise

def start_guacamole_container(network):
    try:
        guacamole_containers = docker_client.containers.list(all=True, filters={'name': 'guacamole'})
        if guacamole_containers:
            container = guacamole_containers[0]
            if container.status != 'running':
                container.start()
            return container
        
        container = docker_client.containers.run(
            'guacamole/guacamole:1.5.5',
            name='guacamole',
            detach=True,
            environment={
                'GUACD_HOSTNAME': 'guacd',
                'MYSQL_HOSTNAME': 'guacamole-mysql',
                'MYSQL_DATABASE': 'guacamole_db',
                'MYSQL_USER': 'guacamole_user',
                'MYSQL_PASSWORD': 'guacamole_pass'
            },
            ports={'8080/tcp': 8080}
        )
        network.connect(container)
        return container
    except Exception as e:
        logging.error(f"Error starting guacamole container: {e}")
        raise

async def stop_containers(request):
    try:
        if not docker_client:
            return web.json_response({'status': 'error', 'message': 'Docker client not initialized'})
        
        containers = docker_client.containers.list(all=True, filters={'name': ['guacamole', 'guacd', 'guacamole-mysql']})
        for container in containers:
            container.stop()
        
        return web.json_response({'status': 'success', 'message': 'Containers stopped successfully'})
    except Exception as e:
        logging.error(f"Error stopping containers: {e}")
        return web.json_response({'status': 'error', 'message': str(e)})

async def get_status(request):
    status = await get_container_status()
    return web.json_response(status)

async def create_connection(request):
    if not docker_client:
        return web.json_response({'status': 'error', 'message': 'Docker client not initialized'})
    
    try:
        data = await request.json()
        protocol = data.get('protocol', 'RDP').lower()
        host = data.get('host', '')
        port = data.get('port', 3389 if protocol == 'rdp' else (22 if protocol == 'ssh' else 5900))
        username = data.get('username', '')
        password = data.get('password', '')
        name = data.get('name', f'{protocol.upper()} - {host}')
        
        # 驗證主機名不為空
        if not host or host.strip() == '':
            return web.json_response({'status': 'error', 'message': '主機名不能為空'})
        
        auth_data = {'username': 'guacadmin', 'password': 'guacadmin'}
        response = requests.post('http://localhost:8080/guacamole/api/tokens', data=auth_data)
        if response.status_code != 200:
            return web.json_response({'status': 'error', 'message': f'無法獲取 Guacamole API 令牌，狀態碼: {response.status_code}'})
        
        token_data = response.json()
        auth_token = token_data.get('authToken')
        
        connection_data = {
            'parentIdentifier': 'ROOT',
            'name': name,
            'protocol': protocol,
            'parameters': {},
            'attributes': {
                'max-connections': '1',
                'max-connections-per-user': '1',
                'guacd-hostname': 'guacd',
                'guacd-port': '4822'
            }
        }
        
        if protocol == 'rdp':
            connection_data['parameters'] = {
                'hostname': host,
                'port': str(port),
                'username': username,
                'password': password,
                'security': 'nla',
                'ignore-cert': 'true',
                'enable-drive': 'true',
                'create-drive-path': 'true'
            }
        elif protocol == 'ssh':
            connection_data['parameters'] = {
                'hostname': host,
                'port': str(port),
                'username': username,
                'password': password,
                'font-size': '12',
                'color-scheme': 'gray-black',
                'enable-sftp': 'true'
            }
        elif protocol == 'vnc':
            connection_data['parameters'] = {
                'hostname': host,
                'port': str(port),
                'password': password,
                'autoretry': 'true'
            }
        
        headers = {'Content-Type': 'application/json'}
        create_url = f'http://localhost:8080/guacamole/api/session/data/mysql/connections?token={auth_token}'
        create_response = requests.post(create_url, json=connection_data, headers=headers)
        if create_response.status_code != 200:
            return web.json_response({'status': 'error', 'message': f'無法創建連接，狀態碼: {create_response.status_code}'})
        
        return web.json_response({
            'status': 'success', 
            'message': f'成功創建連接 {name}',
            'connection_id': create_response.json()
        })
    except Exception as e:
        logging.error(f"Error creating connection: {e}")
        return web.json_response({'status': 'error', 'message': str(e)})

async def list_connections(request):
    try:
        auth_data = {'username': 'guacadmin', 'password': 'guacadmin'}
        response = requests.post('http://localhost:8080/guacamole/api/tokens', data=auth_data)
        if response.status_code != 200:
            return web.json_response({'status': 'error', 'message': f'無法獲取 Guacamole API 令牌，狀態碼: {response.status_code}'})
        
        token_data = response.json()
        auth_token = token_data.get('authToken')
        
        list_url = f'http://localhost:8080/guacamole/api/session/data/mysql/connections?token={auth_token}'

        list_response = requests.get(list_url)
        if list_response.status_code != 200:
            return web.json_response({'status': 'error', 'message': f'無法獲取連接列表，狀態碼: {list_response.status_code}'})
        
        connections = list_response.json()
        return web.json_response({'status': 'success', 'connections': connections})
    except Exception as e:
        logging.error(f"Error listing connections: {e}")
        return web.json_response({'status': 'error', 'message': str(e)})

async def execute_command(request):
    try:
        data = await request.json()
        connection_id = data.get('connection_id')
        command = data.get('command')
        
        token_response = await get_guacamole_token(None)
        token_data = json.loads(token_response.text)
        token = token_data.get('token')
        
        result = await session_manager.execute_command(connection_id, command, token)
        return web.json_response(result)
    except Exception as e:
        logging.error(f"Error in execute_command: {e}")
        return web.json_response({'status': 'error', 'message': str(e)})

async def execute_script(request):
    try:
        data = await request.json()
        connection_id = data.get('connection_id')
        script = data.get('script')
        
        token_response = await get_guacamole_token(None)
        token_data = json.loads(token_response.text)
        token = token_data.get('token')
        
        results = await session_manager.execute_script(connection_id, script, token)
        return web.json_response({'status': 'success', 'results': results})
    except Exception as e:
        logging.error(f"Error in execute_script: {e}")
        return web.json_response({'status': 'error', 'message': str(e)})

async def get_guacamole_token(request):
    try:
        auth_data = {'username': 'guacadmin', 'password': 'guacadmin'}
        response = requests.post('http://localhost:8080/guacamole/api/tokens', data=auth_data)
        if response.status_code != 200:
            return web.json_response({'status': 'error', 'message': '無法獲取Guacamole API令牌'})
        
        token_data = response.json()
        return web.json_response({'status': 'success', 'token': token_data['authToken']})
    except Exception as e:
        logging.error(f"Error getting Guacamole token: {e}")
        return web.json_response({'status': 'error', 'message': str(e)})

async def get_scripts(request):
    scripts = [
        {"id": "open_cmd", "name": "打開命令提示符", "description": "打開Windows命令提示符", "icon": "icon-terminal", "platform": "windows"},
        {"id": "screenshot", "name": "截圖", "description": "啟動Windows截圖工具", "icon": "icon-camera", "platform": "windows"},
        {"id": "browser", "name": "打開瀏覽器", "description": "打開默認瀏覽器", "icon": "icon-globe", "platform": "all"},
        {"id": "notepad", "name": "打開記事本", "description": "打開Windows記事本", "icon": "icon-file-text", "platform": "windows"},
        {"id": "explorer", "name": "文件資源管理器", "description": "打開Windows文件資源管理器", "icon": "icon-folder", "platform": "windows"}
    ]
    return web.json_response({'status': 'success', 'scripts': scripts})

async def websocket_handler(request):
    # 增加ping_interval和ping_timeout參數，延長超時時間
    ws = web.WebSocketResponse(heartbeat=45, autoping=True, timeout=60)
    await ws.prepare(request)
    
    # 添加一個定期發送ping的任務
    async def send_ping():
        while not ws.closed:
            await asyncio.sleep(30)  # 增加到30秒
            if not ws.closed:
                await ws.ping()
                logging.debug("Sent WebSocket ping")
    
    ping_task = asyncio.create_task(send_ping())
    
    connection_id = None
    controller = None
    
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    cmd = data.get('cmd')
                    
                    # 添加日誌以便調試
                    logging.debug(f"收到WebSocket命令: {cmd}")
                    
                    if cmd == 'ping':
                        # 處理客戶端的ping請求
                        await ws.send_json({'type': 'pong', 'timestamp': int(time.time() * 1000)})
                    elif cmd == 'connect':
                        connection_id = data.get('connection_id')
                        
                        # 檢查是否已經有相同連接ID的活躍會話
                        if connection_id in session_manager.active_sessions:
                            # 檢查現有會話是否仍然有效
                            existing_controller = session_manager.active_sessions[connection_id]
                            if existing_controller.automator.connected:
                                logging.info(f"重用現有的連接會話: {connection_id}")
                                controller = existing_controller
                                
                                # 更新WebSocket連接
                                session_manager.register_websocket(connection_id, ws)
                                
                                # 設置指令發送函數
                                loop = asyncio.get_event_loop()
                                def instruction_poster(opcode, args):
                                    async def _send():
                                        if not ws.closed:
                                            await ws.send_json({
                                                'type': 'guac-instruction',
                                                'opcode': opcode,
                                                'args': list(args)
                                            })
                                    asyncio.run_coroutine_threadsafe(_send(), loop)
                                
                                controller.automator.instruction_poster_func = instruction_poster
                                await ws.send_json({'status': 'success', 'message': 'Reusing existing connection'})
                                continue
                        
                        # 如果沒有有效的現有會話，則創建新會話
                        token_response = await get_guacamole_token(None)
                        token_data = json.loads(token_response.text)
                        token = token_data.get('token')
                        
                        try:
                            controller = await session_manager.get_or_create_session(connection_id, token)
                            if controller:
                                session_manager.register_websocket(connection_id, ws)
                                loop = asyncio.get_event_loop()
                                def instruction_poster(opcode, args):
                                    async def _send():
                                        if not ws.closed:
                                            await ws.send_json({
                                                'type': 'guac-instruction',
                                                'opcode': opcode,
                                                'args': list(args)
                                            })
                                    asyncio.run_coroutine_threadsafe(_send(), loop)
                                controller.automator.instruction_poster_func = instruction_poster
                                await ws.send_json({'status': 'success', 'message': 'Connection established'})
                            else:
                                await ws.send_json({'status': 'error', 'message': 'Failed to establish connection'})
                        except Exception as e:
                            await ws.send_json({'status': 'error', 'message': f'Failed to establish connection: {str(e)}'})
                    
                    elif cmd == 'execute':
                        if not connection_id or not controller:
                            await ws.send_json({'status': 'error', 'message': 'No active connection'})
                            continue
                        
                        command = data.get('command', '')
                        if not command:  # 檢查命令是否為空
                            await ws.send_json({'status': 'error', 'message': 'Empty command'})
                            continue
                        
                        # 修正問題2：確保鍵盤狀態正確傳遞
                        if command.startswith('key '):
                            parts = command.split(' ')
                            # 確保狀態參數是1或0
                            if len(parts) > 2:
                                keysym = parts[1]
                                state = parts[2]
                                # 確保狀態是1或0
                                if state not in ['0', '1']:
                                    state = '1' if state.lower() in ['true', 'down'] else '0'
                                command = f"key {keysym} {state}"
                            
                            asyncio.create_task(controller.execute_command(command))
                            await ws.send_json({'status': 'success', 'result': f"Command executed: {command}"})
                        else:
                            try:
                                result = await controller.execute_command(command)
                                if result is True:  # 如果結果只是布爾值True
                                    await ws.send_json({'status': 'success', 'message': f"Command executed: {command}"})
                                else:
                                    await ws.send_json({'status': 'success', 'result': result or f"Command executed: {command}"})
                            except Exception as e:
                                logging.error(f"命令執行失敗: {str(e)}")
                                await ws.send_json({'status': 'error', 'message': f'Command execution failed: {str(e)}'})
                                
                    elif cmd == 'execute_script':
                        if not connection_id or not controller:
                            await ws.send_json({'status': 'error', 'message': 'No active connection'})
                            continue
                        
                        script = data.get('script', '')
                        if not script:
                            await ws.send_json({'status': 'error', 'message': 'Empty script'})
                            continue
                        
                        try:
                        
                            token_response = await get_guacamole_token(None)
                            token_data = json.loads(token_response.text)
                            token = token_data.get('token')
                            # 修正問題3：改進預定義腳本處理
                            if script.startswith('script '):
                                script_name = script.split(' ')[1]
                                if script_name == 'open_cmd':
                                    script = """
key win 1
wait 0.1
key win 0
wait 0.2
key r 1
wait 0.1
key r 0
wait 0.5
type cmd
wait 0.2
key return 1
wait 0.1
key return 0
"""
                                elif script_name == 'screenshot':
                                    script = """
key win 1
wait 0.1
key shift 1
wait 0.1
key s 1
wait 0.1
key s 0
wait 0.1
key shift 0
wait 0.1
key win 0
"""
                                elif script_name == 'browser':
                                    script = """
key win 1
wait 0.1
key win 0
wait 0.2
key r 1
wait 0.1
key r 0
wait 0.5
type chrome
wait 0.2
key return 1
wait 0.1
key return 0
"""
                                elif script_name == 'notepad':
                                    script = """
key win 1
wait 0.1
key win 0
wait 0.2
key r 1
wait 0.1
key r 0
wait 0.5
type notepad
wait 0.2
key return 1
wait 0.1
key return 0
"""
                                elif script_name == 'explorer':
                                    script = """
key win 1
wait 0.1
key e 1
wait 0.1
key e 0
wait 0.1
key win 0
"""
                            
                            results = await session_manager.execute_script(connection_id, script, token, ws)
                            await ws.send_json({'status': 'success', 'message': 'Script execution completed'})
                        except Exception as e:
                            logging.error(f"腳本執行失敗: {str(e)}")
                            await ws.send_json({'status': 'error', 'message': f'Script execution failed: {str(e)}'})
                    
                    elif cmd == 'disconnect':
                        if connection_id:
                            # 不要關閉會話，只是取消註冊WebSocket
                            session_manager.unregister_websocket(connection_id)
                            connection_id = None
                            controller = None
                            await ws.send_json({'status': 'success', 'message': 'Connection closed'})
                    
                    else:
                        await ws.send_json({'status': 'error', 'message': f'Unknown command: {cmd}'})
                except json.JSONDecodeError:
                    await ws.send_json({'status': 'error', 'message': 'Invalid JSON data'})
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logging.error(f'WebSocket connection closed with exception {ws.exception()}')
    
    except Exception as e:
        logging.error(f"WebSocket錯誤: {str(e)}")
        if not ws.closed:
            await ws.send_json({'status': 'error', 'message': f'Server error: {str(e)}'})
    finally:
        ping_task.cancel()  # 確保取消ping任務
        if connection_id:
            session_manager.unregister_websocket(connection_id)
        if not ws.closed:
            await ws.close()
    
    return ws

async def display_handler(request):
    """處理遠程顯示請求，整合自test.py"""
    connection_id = request.query.get('id')
    embedded = request.query.get('embedded', 'false') == 'true'
    
    if not connection_id:
        return web.Response(text="Missing connection ID", status=400)
    
    # 從test.py移植的HTML生成邏輯
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Guacamole Display</title>
        <style>
            body {{ margin: 0; display: flex; justify-content: center; align-items: center; min-height: 100vh; background-color: #282c34; overflow: hidden;}}
            #displayContainer {{ position: relative; }}
            canvas {{ border: 1px solid #444; background-color: #000; image-rendering: pixelated; }}
            #mouseCursor {{
                position: absolute;
                width: 20px; height: 20px;
                pointer-events: none;
                background-image: url('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAAXNSR0IArs4c6QAAACBjSFJNAAB6JgAAgIQAAPoAAACA6AAAdTAAAOpgAAA6mAAAF3CculE8AAAAAmJLR0QA/4ePzL8AAAAJcEhZcwAACxMAAAsTAQCanBgAAAAHdElNRQfhCAoTKw0L12RjAAABFklEQVQ4y63TsUtCURjG8d+xjZAPkbEIU0SRqYNLg4NBi6FDt4P/gEOTQ5uDS5tCk5vA4CAqImL4pEAxUYgIFKKYKFVUxBUVPyL4fl4Tz5w53BvO3XPO4RxnHGfNzrk5366GPYUu3jLNIx7xcY8N5vl/tStYxiROcX4VGeMMV5jGVLaxjP+MaS0Lq9jGN04q3sEdNnGJEUxrRjGNadxiGTc4w3p9pBjfOYkNPGMSc5jGEq7xjU2M4lHGNK4xiz00Z5TQCvawh028YUhjK/s4wSYmcYITnOAFe9jCGI4wjw284wSnWMYt5nCDJRzhEic4w1mcY02f4QqLOEaNn3/ET3zFKT5hEnP4xZk28YwJLuEedzjbKz7iHc4wwcEFLpMAAAAASUVORK5CYII=');
                background-repeat: no-repeat; background-size: contain;
                z-index: 1000; display: none;
            }}
            /* 性能優化設置 */
            #display {{
                will-change: transform;
                transform: translateZ(0);
            }}
            #status {{
                position: fixed;
                bottom: 10px;
                left: 10px;
                color: #fff;
                background: rgba(0,0,0,0.5);
                padding: 5px;
                border-radius: 3px;
                font-family: monospace;
                z-index: 1000;
            }}
            #keyboardStatus {{
                position: fixed;
                top: 10px;
                left: 10px;
                color: #fff;
                background: rgba(0,0,0,0.5);
                padding: 5px;
                border-radius: 3px;
                font-family: monospace;
                z-index: 1000;
            }}
            #keyboardInput {{
                position: absolute;
                opacity: 0;
                top: 0;
                left: 0;
                width: 2px;
                height: 2px;
                z-index: -1;
            }}
            #connectionInfo {{
                position: fixed;
                top: 10px;
                right: 10px;
                color: #fff;
                background: rgba(0,0,0,0.5);
                padding: 5px;
                border-radius: 3px;
                font-family: monospace;
                z-index: 1000;
            }}
        </style>
    </head>
    <body>
        <div id="displayContainer">
            <canvas id="display" tabindex="0"></canvas>
            <div id="mouseCursor"></div>
            <input id="keyboardInput" type="text" autocomplete="off">
        </div>
        <div id="status">連接中...</div>
        <div id="keyboardStatus">點擊畫布啟用鍵盤</div>
        <div id="connectionInfo">連接ID: {connection_id}</div>

        <script>
            // 性能優化設置
            const PERFORMANCE_MODE = true; // 啟用性能模式
            const BATCH_UPDATES = true;    // 批量更新
            
            // WebSocket連接配置
            const WS_RECONNECT_DELAY = 2000; // 重連延遲時間(毫秒)
            const WS_MAX_RECONNECT_ATTEMPTS = 10; // 最大重連次數
            const WS_PING_INTERVAL = 15000; // 心跳間隔(毫秒)
            
            let reconnectAttempts = 0;
            let pingTimer = null;
            let ws = null;
            let isReconnecting = false;
            
            const displayContainer = document.getElementById('displayContainer');
            const canvas = document.getElementById('display');
            const context = canvas.getContext('2d', {{
                alpha: false,              // 禁用 alpha 通道以提高性能
                desynchronized: true       // 減少延遲
            }});
            const mouseCursorElement = document.getElementById('mouseCursor');
            const statusElement = document.getElementById('status');
            const keyboardStatusElement = document.getElementById('keyboardStatus');
            const keyboardInput = document.getElementById('keyboardInput');
            const connectionInfoElement = document.getElementById('connectionInfo');

            canvas.width = 1080; canvas.height = 768;
            context.fillStyle = 'black';
            context.fillRect(0, 0, canvas.width, canvas.height);

            // 確保畫布可以接收鍵盤焦點
            canvas.setAttribute('tabindex', '0');
            
            // 修正問題4：只在用戶點擊畫布時獲取焦點
            function focusKeyboard() {{
                keyboardInput.focus();
                keyboardStatusElement.textContent = "鍵盤已啟用";
                keyboardStatusElement.style.backgroundColor = "rgba(0,128,0,0.5)";
            }}

            // 只在點擊畫布時獲取焦點
            canvas.addEventListener('click', focusKeyboard);
            
            // 當焦點丟失時提示用戶
            keyboardInput.addEventListener('blur', () => {{
                keyboardStatusElement.textContent = "鍵盤焦點已丟失! 點擊畫布重新獲取";
                keyboardStatusElement.style.backgroundColor = "rgba(255,0,0,0.5)";
            }});

            const layers = {{ 0: context }}; // 簡單的圖層管理
            let currentCompositeOperation = 'source-over';

            // 性能跟踪
            let frameCount = 0;
            let lastFpsTime = performance.now();
            let fps = 0;

            // 批量處理相關變量
            let pendingDrawOperations = [];
            let animationFrameRequested = false;

            // 流處理
            const activeStreams = {{}};

            // 連接到指定的連接ID
            const connectionId = '{connection_id}';
            
            // 初始化WebSocket連接
            function initWebSocket() {{
                if (isReconnecting) return;
                
                if (ws) {{
                    try {{
                        ws.close();
                    }} catch (e) {{
                        console.error("關閉舊WebSocket時出錯:", e);
                    }}
                }}
                
                const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                const wsUrl = `${{wsProtocol}}//${{window.location.host}}/plugin/guacamole/ws`;
                
                try {{
                    ws = new WebSocket(wsUrl);
                    
                    // 設置較長的超時時間
                    ws.timeout = 60000; // 60秒
                    
                    ws.onopen = handleWebSocketOpen;
                    ws.onmessage = handleWebSocketMessage;
                    ws.onclose = handleWebSocketClose;
                    ws.onerror = handleWebSocketError;
                    
                    statusElement.textContent = '正在連接...';
                    statusElement.style.backgroundColor = 'rgba(255,165,0,0.5)'; // 橙色
                }} catch (e) {{
                    console.error("創建WebSocket時出錯:", e);
                    scheduleReconnect();
                }}
            }}
            
            // 處理WebSocket連接成功
            function handleWebSocketOpen() {{
                console.log('WebSocket: 已連接');
                statusElement.textContent = '已連接';
                statusElement.style.backgroundColor = 'rgba(0,128,0,0.5)';
                reconnectAttempts = 0;
                isReconnecting = false;
                
                // 啟動ping定時器
                startPingTimer();
                
                // 發送連接命令
                sendWebSocketMessage({{
                    cmd: 'connect',
                    connection_id: connectionId
                }});
            }}
            
            // 處理WebSocket消息
            function handleWebSocketMessage(event) {{
                try {{
                    const data = JSON.parse(event.data);
                    
                    if (data.status === 'success') {{
                        console.log('成功:', data.message || data.result);
                    }} else if (data.status === 'error') {{
                        console.error('錯誤:', data.message);
                        statusElement.textContent = '錯誤: ' + data.message;
                        statusElement.style.backgroundColor = 'rgba(255,0,0,0.5)';
                    }} else if (data.type === 'guac-instruction') {{
                        // 處理 Guacamole 指令
                        const opcode = data.opcode;
                        const args = data.args;
                        
                        // 處理特殊的 img 和 blob 指令
                        if (opcode === 'img') {{
                            const streamIndex = args[0];
                            const layerIndex = parseInt(args[1]);
                            const mimetype = args[3] || 'image/png';
                            const x = parseInt(args[4] || 0);
                            const y = parseInt(args[5] || 0);
                            
                            // 初始化流
                            activeStreams[streamIndex] = {{ 
                                mimetype: mimetype, 
                                x: x, 
                                y: y, 
                                layerIndex: layerIndex,
                                dataParts: [] 
                            }};
                            return;
                        }}
                        
                        if (opcode === 'blob') {{
                            const streamIndex = args[0];
                            const blobData = args[1];
                            
                            if (activeStreams[streamIndex]) {{
                                activeStreams[streamIndex].dataParts.push(blobData);
                            }}
                            return;
                        }}
                        
                        if (opcode === 'end') {{
                            const streamIndex = args[0];
                            if (activeStreams[streamIndex]) {{
                                const stream = activeStreams[streamIndex];
                                const fullBase64Data = stream.dataParts.join('');
                                
                                // 處理圖像數據
                                if (BATCH_UPDATES && PERFORMANCE_MODE) {{
                                    pendingDrawOperations.push(() => {{
                                        processStreamEnd(stream, fullBase64Data);
                                    }});
                                    requestRender();
                                }} else {{
                                    processStreamEnd(stream, fullBase64Data);
                                }}
                                
                                // 清理流
                                delete activeStreams[streamIndex];
                            }}
                            return;
                        }}

                        // 處理其他指令
                        if (BATCH_UPDATES && PERFORMANCE_MODE &&
                            (opcode === 'png' || opcode === 'jpeg' || opcode === 'cfill' || 
                            opcode === 'copy' || opcode === 'transfer')) {{
                            // 批量處理繪圖操作
                            pendingDrawOperations.push(() => {{
                                processInstruction(opcode, args);
                            }});
                            requestRender();
                        }} else {{
                            // 直接處理其他指令
                            processInstruction(opcode, args);
                        }}
                    }} else if (data.type === 'pong') {{
                        console.log("Received WebSocket pong:", data.timestamp);
                    }}
                }} catch (e) {{
                    console.error('解析消息失敗:', e, event.data);
                }}
            }}
            
            // 處理WebSocket關閉
            function handleWebSocketClose(event) {{
                console.log('WebSocket: 已斷開', event.code, event.reason);
                statusElement.textContent = `已斷開 (代碼: ${{event.code}})`;
                statusElement.style.backgroundColor = 'rgba(255,0,0,0.5)';
                
                // 清除ping定時器
                stopPingTimer();
                
                // 嘗試重新連接
                scheduleReconnect();
            }}
            
            // 處理WebSocket錯誤
            function handleWebSocketError(error) {{
                console.error('WebSocket錯誤:', error);
                statusElement.textContent = '連接錯誤';
                statusElement.style.backgroundColor = 'rgba(255,0,0,0.5)';
                
                // 清除ping定時器
                stopPingTimer();
                
                // 嘗試重新連接
                scheduleReconnect();
            }}
            
            // 安排重新連接
            function scheduleReconnect() {{
                if (isReconnecting) return;
                
                isReconnecting = true;
                reconnectAttempts++;
                
                if (reconnectAttempts <= WS_MAX_RECONNECT_ATTEMPTS) {{
                    const delay = Math.min(30000, WS_RECONNECT_DELAY * Math.pow(1.5, reconnectAttempts - 1));
                    console.log(`嘗試在 ${{delay}}ms 後重新連接 (嘗試 ${{reconnectAttempts}}/${{WS_MAX_RECONNECT_ATTEMPTS}})`);
                    statusElement.textContent = `嘗試重新連接... (${{reconnectAttempts}}/${{WS_MAX_RECONNECT_ATTEMPTS}})`;
                    
                    setTimeout(() => {{
                        isReconnecting = false;
                        initWebSocket();
                    }}, delay);
                }} else {{
                    console.error('達到最大重連次數，停止重連');
                    statusElement.textContent = '連接失敗，請刷新頁面重試';
                }}
            }}
            
            // 啟動ping定時器
            function startPingTimer() {{
                stopPingTimer();
                pingTimer = setInterval(() => {{
                    if (ws && ws.readyState === WebSocket.OPEN) {{
                        sendWebSocketMessage({{ cmd: 'ping' }});
                        console.log("Sent WebSocket ping");
                    }}
                }}, WS_PING_INTERVAL);
            }}
            
            // 停止ping定時器
            function stopPingTimer() {{
                if (pingTimer) {{
                    clearInterval(pingTimer);
                    pingTimer = null;
                }}
            }}
            
            // 發送WebSocket消息
            function sendWebSocketMessage(data) {{
                if (ws && ws.readyState === WebSocket.OPEN) {{
                    try {{
                        ws.send(JSON.stringify(data));
                    }} catch (e) {{
                        console.error("發送WebSocket消息失敗:", e);
                    }}
                }} else {{
                    console.warn("WebSocket未連接，無法發送消息");
                }}
            }}

            // 請求動畫幀來批量處理繪圖操作
            function requestRender() {{
                if (!animationFrameRequested) {{
                    animationFrameRequested = true;
                    requestAnimationFrame(renderPendingOperations);
                }}
            }}

            // 批量處理所有待處理的繪圖操作
            function renderPendingOperations() {{
                animationFrameRequested = false;
                
                // 執行所有待處理的繪圖操作
                const operationsCount = pendingDrawOperations.length;
                for (let i = 0; i < operationsCount; i++) {{
                    pendingDrawOperations[i]();
                }}
                pendingDrawOperations = [];
                
                // 更新 FPS 計數器
                frameCount++;
                const now = performance.now();
                const elapsed = now - lastFpsTime;
                if (elapsed >= 1000) {{
                    fps = Math.round(frameCount * 1000 / elapsed);
                    lastFpsTime = now;
                    frameCount = 0;
                    if (PERFORMANCE_MODE) {{
                        statusElement.textContent = `已連接 | FPS: ${{fps}}`;
                    }}
                }}
            }}

            // 處理流結束時的圖像渲染
            function processStreamEnd(stream, fullBase64Data) {{
                const ctx = layers[0]; // 使用主畫布
                const img = new Image();
                img.onload = function() {{
                    ctx.drawImage(img, stream.x, stream.y);
                }};
                img.onerror = function() {{
                    console.error(`Failed to load image from stream`);
                }};
                img.src = `data:${{stream.mimetype}};base64,${{fullBase64Data}}`;
            }}

            // 處理 Guacamole 指令
            function processInstruction(opcode, args) {{
                const ctx = layers[0]; // 使用主畫布
                
                switch (opcode) {{
                    case 'size': // args: [layer_index, width, height]
                        const layer = parseInt(args[0]);
                        const width = parseInt(args[1]);
                        const height = parseInt(args[2]);
                        if (layer === 0) {{
                            canvas.width = width;
                            canvas.height = height;
                            console.log(`Canvas resized: ${{width}}x${{height}} for layer ${{layer}}`);
                        }}
                        break;
                    case 'rect': // args: [layer_index, x, y, width, height]
                        ctx.clearRect(parseInt(args[1]), parseInt(args[2]), parseInt(args[3]), parseInt(args[4]));
                        break;
                    case 'cfill': // args: [mask, layer_index, r, g, b, a, x, y, width, height]
                        const cfill_mask = parseInt(args[0]);
                        ctx.fillStyle = `rgba(${{parseInt(args[2])}},${{parseInt(args[3])}},${{parseInt(args[4])}},${{parseInt(args[5])/255}})`;
                        ctx.globalCompositeOperation = getCompositeOperation(cfill_mask);
                        ctx.fillRect(parseInt(args[6]), parseInt(args[7]), parseInt(args[8]), parseInt(args[9]));
                        ctx.globalCompositeOperation = currentCompositeOperation;
                        break;
                    case 'png': // args: [mask, layer_index, x, y, data_base64]
                    case 'jpeg':
                    case 'webp':
                        const img_mask_direct = parseInt(args[0]);
                        const img_x_direct = parseInt(args[2]); // Layer index is args[1]
                        const img_y_direct = parseInt(args[3]);
                        const base64Data_direct = args[4];
                        
                        const img_direct = new Image();
                        img_direct.onload = function() {{
                            ctx.globalCompositeOperation = getCompositeOperation(img_mask_direct);
                            ctx.drawImage(img_direct, img_x_direct, img_y_direct);
                            ctx.globalCompositeOperation = currentCompositeOperation;
                        }};
                        img_direct.onerror = function() {{ console.error(`Failed to load direct ${{opcode}} data.`); }}
                        img_direct.src = `data:image/${{opcode}};base64,` + base64Data_direct;
                        break;
                    case 'copy': // args: [src_layer, sx, sy, sw, sh, mask, dst_layer, dx, dy]
                        const srcL = parseInt(args[0]);
                        const sx = parseInt(args[1]);
                        const sy = parseInt(args[2]);
                        const sw = parseInt(args[3]);
                        const sh = parseInt(args[4]);
                        const copy_mask_val = parseInt(args[5]);
                        const dstL = parseInt(args[6]);
                        const dx = parseInt(args[7]);
                        const dy = parseInt(args[8]);
                        
                        // 處理所有 COPY 指令，包括負數層
                        if (dstL === 0) {{ // 只要目標是主畫布，就嘗試處理
                            ctx.globalCompositeOperation = getCompositeOperation(copy_mask_val);
                            if (srcL === 0) {{
                                // 從主畫布複製到主畫布
                                ctx.drawImage(canvas, sx, sy, sw, sh, dx, dy, sw, sh);
                            }} else if (srcL < 0) {{
                                // 處理負數層 - 通常是預定義的圖像
                                // 繪製一個半透明矩形作為替代
                                ctx.fillStyle = "rgba(200, 200, 200, 0.5)";
                                ctx.fillRect(dx, dy, sw, sh);
                            }}
                            ctx.globalCompositeOperation = currentCompositeOperation;
                        }}
                        break;
                    case 'transfer':
                        const t_srcL = parseInt(args[0]);
                        const t_sx = parseInt(args[1]);
                        const t_sy = parseInt(args[2]);
                        const t_sw = parseInt(args[3]);
                        const t_sh = parseInt(args[4]);
                        const t_mask_val = parseInt(args[5]);
                        const t_dx = parseInt(args[7]);
                        const t_dy = parseInt(args[8]);
                        
                        // 類似於 COPY
                        if (t_srcL === 0) {{
                            ctx.globalCompositeOperation = getCompositeOperation(t_mask_val);
                            ctx.drawImage(canvas, t_sx, t_sy, t_sw, t_sh, t_dx, t_dy, t_sw, t_sh);
                            ctx.globalCompositeOperation = currentCompositeOperation;
                        }} else if (t_srcL < 0) {{
                            // 處理負數層
                            ctx.fillStyle = "rgba(200, 200, 200, 0.5)";
                            ctx.fillRect(t_dx, t_dy, t_sw, t_sh);
                        }}
                        break;
                    case 'cursor': // args: [x, y, src_layer, sx, sy, sw, sh] OR [hotspot_x, hotspot_y, "image/png", base64_data, w, h]
                        const cur_x = parseInt(args[0]);
                        const cur_y = parseInt(args[1]);
                        mouseCursorElement.style.left = (canvas.offsetLeft + cur_x) + 'px';
                        mouseCursorElement.style.top = (canvas.offsetTop + cur_y) + 'px';
                        
                        if (args.length >= 4 && args[2] && args[2].startsWith && args[2].startsWith("image/")) {{
                            const cursorMime = args[2];
                            const cursorData = args[3];
                            mouseCursorElement.style.backgroundImage = `url(data:${{cursorMime}};base64,${{cursorData}})`;
                            if (args.length >= 6) {{ // Optional width/height for cursor image
                                mouseCursorElement.style.width = parseInt(args[4]) + 'px';
                                mouseCursorElement.style.height = parseInt(args[5]) + 'px';
                            }}
                            mouseCursorElement.style.display = 'block';
                        }} else {{
                            mouseCursorElement.style.display = 'block'; // Show default if other type
                        }}
                        break;
                    case 'sync':
                        // 同步指令，不需要特別處理
                        break;
                    case 'nop': 
                        break;
                    case 'error':
                        console.error('Guacamole Server Error:', args.length > 0 ? args[0] : 'Unknown', args.length > 1 ? args[1] : '');
                        statusElement.textContent = `錯誤: ${{args.length > 0 ? args[0] : 'Unknown'}}`;
                        statusElement.style.backgroundColor = 'rgba(255,0,0,0.5)';
                        break;
                    case 'disconnect':
                        console.warn('Guacamole server requested disconnect.');
                        if (ws) ws.close();
                        statusElement.textContent = '伺服器已斷開連接';
                        statusElement.style.backgroundColor = 'rgba(255,0,0,0.5)';
                        break;
                    default:
                        // 忽略未處理的指令，不顯示警告
                        break;
                }}
            }}

            function getCompositeOperation(mask) {{
                // Simplified mapping, see Guacamole protocol for full Porter-Duff operations
                const operations = [
                    "clear",        // 0x00 (ROP_BLACKNESS)
                    "copy",         // 0x01 (ROP_NOTSRCERASE / NOT AND) - 'copy' might be an approximation
                    "destination-in",// 0x02 (ROP_NOTSRCCOPY / AND NOT)
                    "source-over",  // 0x03 (ROP_SRCCOPY - most common)
                    "source-in",    // 0x04 (ROP_SRCERASE / AND)
                    "destination-over",// 0x05 (ROP_DSTINVERT / NOT XOR)
                    "xor",          // 0x06 (ROP_SRCINVERT / XOR)
                    "source-atop",  // 0x07 (ROP_SRCAND / AND)
                    "destination-out",// 0x08 (ROP_MERGEPAINT / OR)
                    "copy",         // 0x09 (ROP_NOTMASKPEN / NOT (MASK AND PEN))
                    "destination-atop",//0x0A (ROP_MASKPENNOT / (PEN) AND (NOT MASK))
                    "source-out",   // 0x0B (ROP_NOTCOPYPEN / NOT (PEN))
                    "copy",         // 0x0C (ROP_MASKPEN / (PEN) AND (MASK))
                    "source-atop",  // 0x0D (ROP_NOTMERGEPEN / NOT (OR PEN))
                    "lighter",      // 0x0E (ROP_MERGEPENNOT / (PEN) OR (NOT MASK)) - 'lighter' for additive
                    "copy"          // 0x0F (ROP_WHITE)
                ];
                return operations[mask] || "source-over";
            }}

            // 鍵盤映射表 - 使用更完整的映射表
            const keyMap = {{
                8: 0xFF08,  // Backspace
                9: 0xFF09,  // Tab
                13: 0xFF0D, // Enter
                16: 0xFFE1, // Shift
                17: 0xFFE3, // Ctrl
                18: 0xFFE9, // Alt
                19: 0xFF13, // Pause/break
                20: 0xFFE5, // Caps lock
                27: 0xFF1B, // Escape
                32: 0x0020, // Space
                33: 0xFF55, // Page up
                34: 0xFF56, // Page down
                35: 0xFF57, // End
                36: 0xFF50, // Home
                37: 0xFF51, // Left arrow
                38: 0xFF52, // Up arrow
                39: 0xFF53, // Right arrow
                40: 0xFF54, // Down arrow
                45: 0xFF63, // Insert
                46: 0xFFFF, // Delete
                48: 0x0030, // 0
                49: 0x0031, // 1
                50: 0x0032, // 2
                51: 0x0033, // 3
                52: 0x0034, // 4
                53: 0x0035, // 5
                54: 0x0036, // 6
                55: 0x0037, // 7
                56: 0x0038, // 8
                57: 0x0039, // 9
                65: 0x0061, // a
                66: 0x0062, // b
                67: 0x0063, // c
                68: 0x0064, // d
                69: 0x0065, // e
                70: 0x0066, // f
                71: 0x0067, // g
                72: 0x0068, // h
                73: 0x0069, // i
                74: 0x006A, // j
                75: 0x006B, // k
                76: 0x006C, // l
                77: 0x006D, // m
                78: 0x006E, // n
                79: 0x006F, // o
                80: 0x0070, // p
                81: 0x0071, // q
                82: 0x0072, // r
                83: 0x0073, // s
                84: 0x0074, // t
                85: 0x0075, // u
                86: 0x0076, // v
                87: 0x0077, // w
                88: 0x0078, // x
                89: 0x0079, // y
                90: 0x007A, // z
                91: 0xFFEB, // Left Windows key
                92: 0xFFEC, // Right Windows key
                93: 0xFF67, // Select key
                96: 0xFFB0, // Numpad 0
                97: 0xFFB1, // Numpad 1
                98: 0xFFB2, // Numpad 2
                99: 0xFFB3, // Numpad 3
                100: 0xFFB4, // Numpad 4
                101: 0xFFB5, // Numpad 5
                102: 0xFFB6, // Numpad 6
                103: 0xFFB7, // Numpad 7
                104: 0xFFB8, // Numpad 8
                105: 0xFFB9, // Numpad 9
                106: 0xFFAA, // Numpad *
                107: 0xFFAB, // Numpad +
                109: 0xFFAD, // Numpad -
                110: 0xFFAE, // Numpad .
                111: 0xFFAF, // Numpad /
                112: 0xFFBE, // F1
                113: 0xFFBF, // F2
                114: 0xFFC0, // F3
                115: 0xFFC1, // F4
                116: 0xFFC2, // F5
                117: 0xFFC3, // F6
                118: 0xFFC4, // F7
                119: 0xFFC5, // F8
                120: 0xFFC6, // F9
                121: 0xFFC7, // F10
                122: 0xFFC8, // F11
                123: 0xFFC9, // F12
                186: 0x003B, // ;
                187: 0x003D, // =
                188: 0x002C, // ,
                189: 0x002D, // -
                190: 0x002E, // .
                191: 0x002F, // /
                192: 0x0060, // `
                219: 0x005B, // [
                220: 0x005C, // \\
                221: 0x005D, // ]
                222: 0x0027  // '
            }};

            // 追蹤按鍵狀態
            const pressedKeys = {{}};
            
            // 修正問題2：確保鍵盤狀態正確傳遞
            // 直接使用input元素捕獲鍵盤輸入 - 優化鍵盤處理
            keyboardInput.addEventListener('keydown', function(event) {{
                const keyCode = event.keyCode;
                let keysym = keyMap[keyCode];
                
                // 如果在映射表中沒有找到，嘗試使用字符編碼
                if (keysym === undefined && event.key && event.key.length === 1) {{
                    keysym = event.key.charCodeAt(0);
                }}
                
                // 確保有有效的 keysym 且該鍵尚未被按下(防止重複觸發)
                if (keysym !== undefined && !pressedKeys[keyCode]) {{
                    pressedKeys[keyCode] = true;
                    sendWebSocketMessage({{
                        cmd: 'execute',
                        command: `key ${{keysym}} 1`
                    }});
                    
                    // 阻止瀏覽器默認行為，但允許複製/粘貼
                    if ([8, 9, 13, 32, 37, 38, 39, 40].includes(keyCode)) {{
                        if (!(event.ctrlKey && ['c', 'v', 'x'].includes(event.key.toLowerCase()))) {{
                            event.preventDefault();
                        }}
                    }}
                }}
            }});
            
            keyboardInput.addEventListener('keyup', function(event) {{
                const keyCode = event.keyCode;
                let keysym = keyMap[keyCode];
                
                // 如果在映射表中沒有找到，嘗試使用字符編碼
                if (keysym === undefined && event.key && event.key.length === 1) {{
                    keysym = event.key.charCodeAt(0);
                }}
                
                // 確保有有效的 keysym 且該鍵已被記錄為按下
                if (keysym !== undefined && pressedKeys[keyCode]) {{
                    delete pressedKeys[keyCode];
                    sendWebSocketMessage({{
                        cmd: 'execute',
                        command: `key ${{keysym}} 0`
                    }});
                }}
            }});

            // 備用鍵盤事件處理 - 全局
            window.addEventListener('keydown', function(event) {{
                // 如果輸入框沒有焦點，則強制獲取焦點
                if (document.activeElement !== keyboardInput) {{
                    keyboardInput.focus();
                }}
                
                const keyCode = event.keyCode;
                let keysym = keyMap[keyCode];
                
                // 如果在映射表中沒有找到，嘗試使用字符編碼
                if (keysym === undefined && event.key && event.key.length === 1) {{
                    keysym = event.key.charCodeAt(0);
                }}
                
                // 確保有有效的 keysym 且該鍵尚未被按下(防止重複觸發)
                if (keysym !== undefined && !pressedKeys[keyCode]) {{
                    pressedKeys[keyCode] = true;
                    sendWebSocketMessage({{
                        cmd: 'execute',
                        command: `key ${{keysym}} 1`
                    }});
                    
                    // 阻止瀏覽器默認行為，但允許複製/粘貼
                    if ([8, 9, 13, 32, 37, 38, 39, 40].includes(keyCode)) {{
                        if (!(event.ctrlKey && ['c', 'v', 'x'].includes(event.key.toLowerCase()))) {{
                            event.preventDefault();
                        }}
                    }}
                }}
            }});
            
            window.addEventListener('keyup', function(event) {{
                const keyCode = event.keyCode;
                let keysym = keyMap[keyCode];
                
                // 如果在映射表中沒有找到，嘗試使用字符編碼
                if (keysym === undefined && event.key && event.key.length === 1) {{
                    keysym = event.key.charCodeAt(0);
                }}
                
                // 確保有有效的 keysym 且該鍵已被記錄為按下
                if (keysym !== undefined && pressedKeys[keyCode]) {{
                    delete pressedKeys[keyCode];
                    sendWebSocketMessage({{
                        cmd: 'execute',
                        command: `key ${{keysym}} 0`
                    }});
                }}
            }});

            // 確保在窗口失去焦點時釋放所有按鍵
            window.addEventListener('blur', function() {{
                // 釋放所有按下的鍵
                for (const keyCode in pressedKeys) {{
                    if (pressedKeys.hasOwnProperty(keyCode)) {{
                        const keysym = keyMap[keyCode];
                        if (keysym !== undefined) {{
                            sendWebSocketMessage({{
                                cmd: 'execute',
                                command: `key ${{keysym}} 0`
                            }});
                        }}
                    }}
                }}
                // 清空按鍵狀態
                for (const key in pressedKeys) {{
                    if (pressedKeys.hasOwnProperty(key)) {{
                        delete pressedKeys[key];
                    }}
                }}
            }});

            // 滑鼠事件處理 - 優化滑鼠處理
            let lastButtonMask = 0;
            
            function getButtonMask(event) {{ 
                let mask = 0; 
                if (event.buttons & 1) mask |= 1; // 左鍵
                if (event.buttons & 2) mask |= 4; // 右鍵
                if (event.buttons & 4) mask |= 2; // 中鍵
                return mask; 
            }}
            
            canvas.addEventListener('mousemove', (event) => {{ 
                const r = canvas.getBoundingClientRect();
                const x = Math.round(event.clientX - r.left);
                const y = Math.round(event.clientY - r.top);
                const bm = getButtonMask(event); 
                sendWebSocketMessage({{
                    cmd: 'execute',
                    command: `mouse ${{x}} ${{y}} ${{bm}} move`
                }});
                lastButtonMask = bm; 
            }});
            
            canvas.addEventListener('mousedown', (event) => {{ 
                // 確保點擊時獲取焦點
                focusKeyboard();
                
                const r = canvas.getBoundingClientRect();
                const x = Math.round(event.clientX - r.left);
                const y = Math.round(event.clientY - r.top);
                const bm = getButtonMask(event); 
                sendWebSocketMessage({{
                    cmd: 'execute',
                    command: `mouse ${{x}} ${{y}} ${{bm}} down`
                }});
                lastButtonMask = bm; 
            }});
            
            canvas.addEventListener('mouseup', (event) => {{ 
                const r = canvas.getBoundingClientRect();
                const x = Math.round(event.clientX - r.left);
                const y = Math.round(event.clientY - r.top);
                let rb = 0; 
                if (event.button === 0) rb = 1; // 左鍵
                else if (event.button === 1) rb = 2; // 中鍵
                else if (event.button === 2) rb = 4; // 右鍵
                const nbm = lastButtonMask & (~rb); 
                sendWebSocketMessage({{
                    cmd: 'execute',
                    command: `mouse ${{x}} ${{y}} ${{nbm}} up`
                }});
                lastButtonMask = nbm; 
            }});
            
            canvas.addEventListener('contextmenu', (event) => event.preventDefault());
            
            // 窗口大小調整處理
            window.addEventListener('resize', () => {{
                // 可選：根據窗口大小調整畫布大小
                // 注意：這會導致重新發送 size 指令給服務器
            }});
            
            // 頁面可見性變化處理
            document.addEventListener('visibilitychange', () => {{
                if (document.visibilityState === 'visible') {{
                    // 頁面變為可見時，嘗試重新連接
                    if (!ws || ws.readyState !== WebSocket.OPEN) {{
                        console.log('頁面變為可見，嘗試重新連接...');
                        initWebSocket();
                    }}
                }}
            }});
            
            // 頁面卸載前清理資源
            window.addEventListener('beforeunload', () => {{
                stopPingTimer();
                if (ws) {{
                    try {{
                        // 發送斷開連接命令
                        sendWebSocketMessage({{ cmd: 'disconnect' }});
                        ws.close();
                    }} catch (e) {{
                        console.error('關閉WebSocket時出錯:', e);
                    }}
                }}
            }});
            
            // 初始化WebSocket連接
            initWebSocket();
        </script>
    </body>
    </html>
    """
    
    return web.Response(text=html_content, content_type='text/html')

