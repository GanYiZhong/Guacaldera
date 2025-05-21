<script setup>
import { ref, onMounted, watch, nextTick, computed, onUnmounted } from "vue";
import axios from "axios";

// 定義響應式變數
const status = ref({
  guacd: '未知',
  guacamole: '未知',
  mysql: '未知'
});

const newConnection = ref({
  name: '',
  protocol: 'RDP',
  host: '',
  port: 3389,
  username: '',
  password: ''
});

// 確保 debugInfo 只宣告一次
const debugInfo = ref('');
const isLoading = ref(false);
const connectionCreated = ref(false);
const connections = ref([]);

// 腳本編輯器相關
const scriptContent = ref('');
const commandOutput = ref([]);
const webSocket = ref(null);
const isConnected = ref(false);
const mouseCursorStyle = ref({ left: '0px', top: '0px' });
const keyboardInput = ref('');
const mousePad = ref(null);
const outputContainer = ref(null);

// 多標籤頁相關
const activeConnections = ref([]);  // 改為數組存儲多個連接
const activeTabIndex = ref(0);      // 當前活動的分頁索引

// 快速命令列表
const quickCommands = ref([
  { id: 'click', name: '左鍵點擊', command: 'mouse 100 100 1 click', icon: 'icon-mouse' },
  { id: 'type', name: '輸入文字', command: 'type Hello World', icon: 'icon-keyboard' },
  { id: 'enter', name: '按回車', command: 'key return', icon: 'icon-return' },
  { id: 'ctrl-alt-del', name: 'Ctrl+Alt+Del', command: 'key ctrl key alt key delete', icon: 'icon-reload' },
  { id: 'win', name: 'Windows鍵', command: 'key win', icon: 'icon-windows' },
  { id: 'esc', name: 'ESC鍵', command: 'key escape', icon: 'icon-x' },
]);

// 特殊按鍵列表
const specialKeys = ref([
  { name: 'Tab', code: 'tab' },
  { name: 'Enter', code: 'return' },
  { name: 'Esc', code: 'escape' },
  { name: 'Ctrl', code: 'ctrl' },
  { name: 'Alt', code: 'alt' },
  { name: 'Shift', code: 'shift' },
  { name: 'Win', code: 'win' },
  { name: '↑', code: 'up' },
  { name: '↓', code: 'down' },
  { name: '←', code: 'left' },
  { name: '→', code: 'right' },
]);

// 獲取預定義腳本
const predefinedScripts = ref([]);

// 錄製和重放功能
const isRecording = ref(false);
const recordedCommands = ref([]);
const recordingStartTime = ref(0);

// 鍵盤映射表
const keySymMap = {
  'return': 0xFF0D,
  'tab': 0xFF09,
  'escape': 0xFF1B,
  'ctrl': 0xFFE3,
  'alt': 0xFFE9,
  'shift': 0xFFE1,
  'win': 0xFFEB,
  'up': 0xFF52,
  'down': 0xFF54,
  'left': 0xFF51,
  'right': 0xFF53,
  'delete': 0xFFFF,
  'backspace': 0xFF08,
  'space': 0x0020,
  'a': 0x0061, 'b': 0x0062, 'c': 0x0063, 'd': 0x0064, 'e': 0x0065, 'f': 0x0066, 'g': 0x0067, 'h': 0x0068,
  'i': 0x0069, 'j': 0x006A, 'k': 0x006B, 'l': 0x006C, 'm': 0x006D, 'n': 0x006E, 'o': 0x006F, 'p': 0x0070,
  'q': 0x0071, 'r': 0x0072, 's': 0x0073, 't': 0x0074, 'u': 0x0075, 'v': 0x0076, 'w': 0x0077, 'x': 0x0078,
  'y': 0x0079, 'z': 0x007A
};

// 獲取鍵碼
const getKeysym = (key) => {
  key = key.toLowerCase();
  if (keySymMap[key]) {
    return keySymMap[key];
  }
  // 如果沒有映射，返回字符的Unicode值
  if (key.length === 1) {
    return key.charCodeAt(0);
  }
  return 0; // 未知鍵
};

// 發送特殊鍵
const sendSpecialKey = (keyCode) => {
  const keysym = getKeysym(keyCode);
  if (keysym) {
    // 修正問題2：確保鍵盤狀態正確傳遞
    executeCommand(`key ${keysym} 1`);
    setTimeout(() => {
      executeCommand(`key ${keysym} 0`);
    }, 100);
  }
};


// 監聽協議變化，自動設置默認端口
watch(() => newConnection.value.protocol, (newProtocol) => {
  switch(newProtocol) {
    case 'RDP':
      newConnection.value.port = 3389;
      break;
    case 'SSH':
      newConnection.value.port = 22;
      break;
    case 'VNC':
      newConnection.value.port = 5900;
      break;
  }
});

const refreshStatus = async () => {
  try {
    isLoading.value = true;
    const { data } = await axios.get('/plugin/guacamole/status');
    status.value = data;
    debugInfo.value = '狀態已更新';
  } catch (error) {
    debugInfo.value = `刷新狀態失敗: ${error}`;
    console.error(error);
  } finally {
    isLoading.value = false;
  }
};

const startService = async () => {
  try {
    isLoading.value = true;
    debugInfo.value = '正在啟動服務...';
    const response = await axios.post('/plugin/guacamole/start');
    const data = response.data;
    debugInfo.value = `啟動成功：${data.message}`;
    await refreshStatus();
  } catch (error) {
    if (error.response) {
      debugInfo.value = `啟動服務失敗：${error.response.data.message}`;
    } else if (error.request) {
      debugInfo.value = '啟動服務失敗：未收到後端響應';
    } else {
      debugInfo.value = `啟動服務失敗：${error.message}`;
    }
    console.error(error);
  } finally {
    isLoading.value = false;
  }
};

const stopService = async () => {
  try {
    isLoading.value = true;
    debugInfo.value = '正在停止服務...';
    const response = await axios.post('/plugin/guacamole/stop');
    const data = response.data;
    debugInfo.value = `停止成功：${data.message}`;
    await refreshStatus();
  } catch (error) {
    if (error.response) {
      debugInfo.value = `停止服務失敗：${error.response.data.message}`;
    } else {
      debugInfo.value = `停止服務失敗：${error}`;
    }
    console.error(error);
  } finally {
    isLoading.value = false;
  }
};

const openGuacamole = () => {
  if (status.value.guacamole !== 'running') {
    debugInfo.value = 'Guacamole 服務未運行，請先啟動服務';
    return;
  }
  window.open('http://localhost:8080/guacamole/');
};

const createConnection = async () => {
  try {
    if (status.value.guacamole !== 'running') {
      debugInfo.value = 'Guacamole 服務未運行，請先啟動服務';
      return;
    }
    
    isLoading.value = true;
    debugInfo.value = '正在創建連接...';
    const res = await axios.post('/plugin/guacamole/create_connection', newConnection.value);
    debugInfo.value = JSON.stringify(res.data, null, 2);
    
    if (res.data.status === 'success') {
      connectionCreated.value = true;
      setTimeout(() => {
        connectionCreated.value = false;
      }, 5000);
      
      // 清空表單
      newConnection.value = {
        name: '',
        protocol: 'RDP',
        host: '',
        port: 3389,
        username: '',
        password: ''
      };
      
      // 刷新連接列表
      await listConnections();
    }
  } catch (error) {
    if (error.response) {
      debugInfo.value = `建立連接失敗：${error.response.data.message}`;
    } else {
      debugInfo.value = `建立連接失敗：${error}`;
    }
    console.error(error);
  } finally {
    isLoading.value = false;
  }
};

const openConnection = async (conn) => {
  if (status.value.guacamole !== 'running') {
    debugInfo.value = 'Guacamole 服務未運行，請先啟動服務';
    return;
  }
  
  try {
    // 獲取 Guacamole 認證令牌
    const tokenResponse = await axios.get('/plugin/guacamole/get_token');
    if (tokenResponse.data.status !== 'success') {
      debugInfo.value = '獲取 Guacamole 令牌失敗';
      return;
    }
    
    // 檢查連接是否已經打開
    const existingIndex = activeConnections.value.findIndex(c => c.identifier === conn.identifier);
    if (existingIndex >= 0) {
      activeTabIndex.value = existingIndex;
      return;
    }
    
    // 添加到活動連接列表
    activeConnections.value.push({
      ...conn,
      type: 'display',  // 使用新的display類型
      url: `/plugin/guacamole/display?id=${conn.identifier}`
    });
    
    // 切換到新打開的分頁
    activeTabIndex.value = activeConnections.value.length - 1;
    
    // 初始化WebSocket連接
    initWebSocket();
  } catch (error) {
    debugInfo.value = `打開連接失敗：${error}`;
    console.error(error);
  }
};


// 添加關閉分頁的函數
const closeTab = async (index) => {
  const conn = activeConnections.value[index];
  
  // 關閉WebSocket連接
  if (webSocket.value && webSocket.value.readyState === WebSocket.OPEN) {
    webSocket.value.send(JSON.stringify({
      cmd: 'disconnect',
      connection_id: conn.identifier
    }));
  }
  
  activeConnections.value.splice(index, 1);
  if (activeTabIndex.value >= activeConnections.value.length) {
    activeTabIndex.value = Math.max(0, activeConnections.value.length - 1);
  }
};

// 切換分頁的函數
const switchTab = (index) => {
  activeTabIndex.value = index;
};

const listConnections = async () => {
  try {
    isLoading.value = true;
    debugInfo.value = '正在獲取連接列表...';
    const response = await axios.get('/plugin/guacamole/list_connections');
    if (response.data.status === 'success') {
      connections.value = Object.values(response.data.connections || {});
      debugInfo.value = '連接列表已更新';
    } else {
      debugInfo.value = `獲取連接列表失敗：${response.data.message}`;
    }
  } catch (error) {
    if (error.response) {
      debugInfo.value = `獲取連接列表失敗：${error.response.data.message}`;
    } else {
      debugInfo.value = `獲取連接列表失敗：${error}`;
    }
    console.error(error);
  } finally {
    isLoading.value = false;
  }
};

// 當前活動連接的ID
const activeConnectionId = computed(() => {
  if (activeTabIndex.value >= 0 && activeConnections.value.length > activeTabIndex.value) {
    return activeConnections.value[activeTabIndex.value].identifier;
  }
  return null;
});

const initWebSocket = () => {
    // 檢查現有連接狀態，如果已連接且狀態正常，則不重新創建
    if (webSocket.value && webSocket.value.readyState === WebSocket.OPEN) {
        console.log("WebSocket 連接已存在且狀態正常，保持現有連接");
        return;
    }
    
    // 如果連接存在但狀態不正常（正在關閉或已關閉），則關閉它
    if (webSocket.value) {
        console.log(`關閉狀態不正常的 WebSocket (readyState: ${webSocket.value.readyState})`);
        webSocket.value.close();
    }
    
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${wsProtocol}//${window.location.host}/plugin/guacamole/ws`;
    
    webSocket.value = new WebSocket(wsUrl);
    
    // 設置心跳間隔
    const pingInterval = 15000; // 15秒
    let pingTimer = null;
    let reconnectAttempts = 0;
    const maxReconnectAttempts = 5;
    
    // 定義ping函數
    const sendPing = () => {
        if (webSocket.value && webSocket.value.readyState === WebSocket.OPEN) {
            webSocket.value.send(JSON.stringify({ cmd: 'ping' }));
            console.log("Sent WebSocket ping");
        }
    };
    
    webSocket.value.onopen = () => {
        isConnected.value = true;
        reconnectAttempts = 0; // 重置重連計數
        addOutputLine('WebSocket連接已建立', 'info');
        
        // 啟動ping定時器
        if (pingTimer) clearInterval(pingTimer);
        pingTimer = setInterval(sendPing, pingInterval);
        
        // 如果有活動連接，立即發送連接命令
        if (activeConnectionId.value) {
            webSocket.value.send(JSON.stringify({
                cmd: 'connect',
                connection_id: activeConnectionId.value
            }));
        }
    };
    
    webSocket.value.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
        
            if (data.status === 'success') {
                if (data.message) {
                    addOutputLine(data.message, 'success');
                }
                if (data.result) {
                    addOutputLine(data.result, 'success');
                }
            } else if (data.status === 'error') {
                addOutputLine(`錯誤: ${data.message || '未知錯誤'}`, 'error');
            } else if (data.type === 'guac-instruction') {
                // 處理 Guacamole 指令，不需要顯示在輸出中
                // 將指令轉發給iframe
                const iframe = document.querySelector('.display-iframe');
                if (iframe && iframe.contentWindow) {
                    iframe.contentWindow.postMessage({
                        type: 'guac-instruction',
                        opcode: data.opcode,
                        args: data.args
                    }, '*');
                }
            } else if (data.type === 'pong') {
                console.log("Received WebSocket pong");
            } else {
                console.warn('收到未知類型的消息:', data);
            }
        } catch (e) {
            console.error('解析WebSocket消息失敗:', e, event.data);
            addOutputLine(`無法解析消息: ${e.message}`, 'error');
        }
    };
    
    webSocket.value.onclose = (event) => {
        isConnected.value = false;
        // 清除ping定時器
        if (pingTimer) {
            clearInterval(pingTimer);
            pingTimer = null;
        }
        
        // 使用簡單的重連策略
        reconnectAttempts++;
        if (reconnectAttempts <= maxReconnectAttempts) {
            const reconnectDelay = 2000 * reconnectAttempts; // 線性增加重連延遲
            addOutputLine(`WebSocket連接已關閉 (代碼: ${event.code})，${reconnectDelay/1000}秒後嘗試重新連接...`, 'info');
            setTimeout(initWebSocket, reconnectDelay);
        } else {
            addOutputLine(`已達最大重連次數 (${maxReconnectAttempts})，請刷新頁面重試`, 'error');
        }
    };
    
    webSocket.value.onerror = (error) => {
        isConnected.value = false;
        addOutputLine(`WebSocket錯誤: ${error}`, 'error');
        console.error('WebSocket error:', error);
        
        // 清除ping定時器
        if (pingTimer) {
            clearInterval(pingTimer);
            pingTimer = null;
        }
    };
};


// 添加輸出行
const addOutputLine = (text, type = 'info') => {
  commandOutput.value.push({ text, type });
  nextTick(() => {
    if (outputContainer.value) {
      outputContainer.value.scrollTop = outputContainer.value.scrollHeight;
    }
  });
};

// 使用防抖動技術處理命令執行
const debounce = (func, wait) => {
  let timeout;
  return function(...args) {
    clearTimeout(timeout);
    timeout = setTimeout(() => func.apply(this, args), wait);
  };
};

const executeCommand = debounce((command) => {
  if (!isConnected.value || !activeConnectionId.value) {
    addOutputLine('無法執行命令：未連接或無活動連接', 'error');
    return;
  }
  
  // 添加日誌以便調試
  console.log(`執行命令: ${command}`);
  
  // 通過WebSocket發送
  if (webSocket.value && webSocket.value.readyState === WebSocket.OPEN) {
    webSocket.value.send(JSON.stringify({
      cmd: 'execute',
      command: command
    }));
  }
  
  // 同時通過iframe消息發送
  const iframe = document.querySelector('.display-iframe');
  if (iframe && iframe.contentWindow) {
    iframe.contentWindow.postMessage({
      type: 'guacamole-command',
      command: command
    }, '*');
  }
  
  addOutputLine(`> ${command}`, 'command');
  
  if (isRecording.value) {
    recordCommand(command);
  }
}, 5); // 5ms 的防抖動延遲

// 執行腳本
const executeScript = async () => {
  if (!scriptContent.value) {
    addOutputLine('腳本內容為空', 'error');
    return;
  }
  
  if (!isConnected.value || !activeConnectionId.value) {
    addOutputLine('無法執行腳本：未連接或無活動連接', 'error');
    return;
  }
  
  addOutputLine('開始執行腳本...', 'info');
  
  try {
    // 先獲取token
    const tokenResponse = await axios.get('/plugin/guacamole/get_token');
    if (tokenResponse.data.status !== 'success') {
      addOutputLine('獲取 Guacamole 令牌失敗', 'error');
      return;
    }
    
    const token = tokenResponse.data.token;
    
    // 發送腳本執行請求
    webSocket.value.send(JSON.stringify({
      cmd: 'execute_script',
      connection_id: activeConnectionId.value,
      script: scriptContent.value,
      token: token  // 添加token參數
    }));
  } catch (error) {
    addOutputLine(`執行腳本失敗: ${error}`, 'error');
  }
};


// 清空腳本
const clearScript = () => {
  scriptContent.value = '';
};

// 保存腳本
const saveScript = () => {
  if (!scriptContent.value) {
    addOutputLine('沒有腳本內容可保存', 'error');
    return;
  }
  
  const scriptName = prompt('請輸入腳本名稱:');
  if (!scriptName) return;
  
  // 保存到localStorage
  const savedScripts = JSON.parse(localStorage.getItem('guacamole_scripts') || '{}');
  savedScripts[scriptName] = scriptContent.value;
  localStorage.setItem('guacamole_scripts', JSON.stringify(savedScripts));
  
  addOutputLine(`腳本 "${scriptName}" 已保存`, 'success');
};

// 載入腳本
const loadScript = () => {
  const savedScripts = JSON.parse(localStorage.getItem('guacamole_scripts') || '{}');
  const scriptNames = Object.keys(savedScripts);
  
  if (scriptNames.length === 0) {
    addOutputLine('沒有保存的腳本', 'error');
    return;
  }
  
  const scriptName = prompt(`請選擇要載入的腳本: ${scriptNames.join(', ')}`);
  if (!scriptName || !savedScripts[scriptName]) {
    addOutputLine('無效的腳本名稱', 'error');
    return;
  }
  
  scriptContent.value = savedScripts[scriptName];
  addOutputLine(`腳本 "${scriptName}" 已載入`, 'success');
};

// 添加命令到腳本
const addCommand = (command) => {
  if (scriptContent.value && !scriptContent.value.endsWith('\n')) {
    scriptContent.value += '\n';
  }
  scriptContent.value += command + '\n';
};

// 運行預定義腳本
const runPredefinedScript = (scriptId) => {
  // 修正問題3：修改預定義腳本的執行方式
  // 根據scriptId生成對應的腳本內容
  let scriptContent = '';
  
  switch(scriptId) {
    case 'open_cmd':
      scriptContent = `key win 1\nwait 0.1\nkey win 0\nwait 0.2\nkey r 1\nwait 0.1\nkey r 0\nwait 0.5\ntype cmd\nwait 0.2\nkey return 1\nwait 0.1\nkey return 0`;
      break;
    case 'screenshot':
      scriptContent = `key win 1\nwait 0.1\nkey shift 1\nwait 0.1\nkey s 1\nwait 0.1\nkey s 0\nwait 0.1\nkey shift 0\nwait 0.1\nkey win 0`;
      break;
    case 'browser':
      scriptContent = `key win 1\nwait 0.1\nkey win 0\nwait 0.2\nkey r 1\nwait 0.1\nkey r 0\nwait 0.5\ntype chrome\nwait 0.2\nkey return 1\nwait 0.1\nkey return 0`;
      break;
    case 'notepad':
      scriptContent = `key win 1\nwait 0.1\nkey win 0\nwait 0.2\nkey r 1\nwait 0.1\nkey r 0\nwait 0.5\ntype notepad\nwait 0.2\nkey return 1\nwait 0.1\nkey return 0`;
      break;
    case 'explorer':
      scriptContent = `key win 1\nwait 0.1\nkey e 1\nwait 0.1\nkey e 0\nwait 0.1\nkey win 0`;
      break;
    default:
      addOutputLine(`未知的預定義腳本: ${scriptId}`, 'error');
      return;
  }
  
  // 執行生成的腳本
  const lines = scriptContent.split('\n');
  let delay = 0;
  
  lines.forEach((line) => {
    setTimeout(() => {
      executeCommand(line);
    }, delay);
    delay += 200; // 每條命令間隔200ms
  });
  
  addOutputLine(`執行預定義腳本: ${scriptId}`, 'info');
};

// 清空輸出
const clearOutput = () => {
  commandOutput.value = [];
};

// 發送鍵盤輸入
const sendKeyboardInput = () => {
  if (!keyboardInput.value) return;
  
  executeCommand(`type ${keyboardInput.value}`);
  keyboardInput.value = '';
};

// 鼠標控制相關函數
const onMousePadMove = (event) => {
  if (!mousePad.value) return;
  
  const rect = mousePad.value.getBoundingClientRect();
  const x = Math.floor(event.clientX - rect.left);
  const y = Math.floor(event.clientY - rect.top);
  
  // 更新鼠標游標位置
  mouseCursorStyle.value = {
    left: `${x}px`,
    top: `${y}px`
  };
  
  // 發送鼠標移動命令
  if (isConnected.value && activeConnectionId.value) {
    executeCommand(`mouse ${x} ${y} 0 move`);
  }
};

const onMousePadDown = (event) => {
  if (!mousePad.value) return;
  
  const rect = mousePad.value.getBoundingClientRect();
  const x = Math.floor(event.clientX - rect.left);
  const y = Math.floor(event.clientY - rect.top);
  
  // 發送鼠標按下命令
  executeCommand(`mouse ${x} ${y} 1 down`);
};

const onMousePadUp = (event) => {
  if (!mousePad.value) return;
  
  const rect = mousePad.value.getBoundingClientRect();
  const x = Math.floor(event.clientX - rect.left);
  const y = Math.floor(event.clientY - rect.top);
  
  // 發送鼠標釋放命令
  executeCommand(`mouse ${x} ${y} 0 up`);
};


const onMousePadLeave = (event) => {
  // 鼠標離開區域時釋放按鈕
  executeCommand(`mouse 0 0 1 up`);
};

const onMouseButtonEvent = (button, action) => {
  if (!mousePad.value) return;
  
  // 獲取當前鼠標位置
  const style = mouseCursorStyle.value;
  const x = parseInt(style.left) || 0;
  const y = parseInt(style.top) || 0;
  
  // 發送鼠標按鈕命令
  executeCommand(`mouse ${x} ${y} ${button} ${action}`);
};

const fetchPredefinedScripts = async () => {
  try {
    const response = await axios.get('/plugin/guacamole/scripts');
    if (response.data.status === 'success') {
      predefinedScripts.value = response.data.scripts;
    }
  } catch (error) {
    console.error('Failed to fetch predefined scripts:', error);
  }
};

// 開始錄製
const startRecording = () => {
  if (isRecording.value) {
    // 如果正在錄製，則停止錄製
    isRecording.value = false;
    addOutputLine(`錄製完成，共記錄了 ${recordedCommands.value.length} 條命令`, 'success');
  } else {
    // 開始錄製
    recordedCommands.value = [];
    recordingStartTime.value = Date.now();
    isRecording.value = true;
    addOutputLine('開始錄製操作...', 'info');
  }
};

// 記錄命令
const recordCommand = (command) => {
  if (isRecording.value) {
    const timestamp = Date.now() - recordingStartTime.value;
    recordedCommands.value.push({ timestamp, command });
  }
};

// 重放錄製
const replayRecording = () => {
  if (recordedCommands.value.length === 0) {
    addOutputLine('沒有可重放的命令', 'error');
    return;
  }
  
  addOutputLine('開始重放錄製的操作...', 'info');
  
  let previousTime = 0;
  
  recordedCommands.value.forEach((record, index) => {
    const delay = index === 0 ? 0 : record.timestamp - previousTime;
    previousTime = record.timestamp;
    
    setTimeout(() => {
      executeCommand(record.command);
    }, delay);
  });
};

// 組件生命週期鉤子
onMounted(() => {
  refreshStatus();
  listConnections();
  fetchPredefinedScripts();
  
  // 添加窗口消息監聽器，用於處理iframe的消息
  window.addEventListener('message', (event) => {
    // 確保消息來源是我們的iframe
    const iframe = document.querySelector('.display-iframe');
    if (iframe && event.source === iframe.contentWindow) {
      if (event.data.type === 'guacamole-event') {
        // 處理來自iframe的事件
        console.log('Received event from iframe:', event.data);
        if (event.data.event === 'keydown' || event.data.event === 'keyup') {
          // 處理鍵盤事件
          const keysym = event.data.keysym;
          const state = event.data.event === 'keydown' ? '1' : '0';
          executeCommand(`key ${keysym} ${state}`);
        } else if (event.data.event === 'mousemove' || event.data.event === 'mousedown' || event.data.event === 'mouseup') {
          // 處理鼠標事件
          const x = event.data.x;
          const y = event.data.y;
          const button = event.data.button || 0;
          const action = event.data.event.replace('mouse', '');
          executeCommand(`mouse ${x} ${y} ${button} ${action}`);
        }
      }
    }
  });
});

onUnmounted(() => {
  if (webSocket.value) {
    webSocket.value.close();
  }
  
  // 移除窗口消息監聽器
  window.removeEventListener('message', () => {});
});

// 當切換標籤頁時，更新WebSocket連接
watch(activeTabIndex, (newIndex) => {
    if (newIndex >= 0 && activeConnections.value.length > newIndex) {
        const newConnId = activeConnections.value[newIndex].identifier;
        
        // 檢查是否需要切換連接
        if (isConnected.value) {
            // 如果WebSocket連接正常且當前沒有活動連接ID，或者連接ID不同，則發送新的連接命令
            if (!activeConnectionId.value || activeConnectionId.value !== newConnId) {
                console.log(`切換連接從 ${activeConnectionId.value || 'none'} 到 ${newConnId}`);
                
                // 發送連接命令而不是先斷開再連接
                webSocket.value.send(JSON.stringify({
                    cmd: 'connect',
                    connection_id: newConnId
                }));
            } else {
                console.log(`保持當前連接 ${activeConnectionId.value}`);
            }
        } else if (webSocket.value && webSocket.value.readyState !== WebSocket.OPEN) {
            // 如果WebSocket未連接，則初始化連接
            console.log("WebSocket未連接，重新初始化");
            initWebSocket();
        }
    }
});

const checkConnectionHealth = () => {
    // 如果連接應該是活躍的但WebSocket狀態不正常，則重新連接
    if (isConnected.value && webSocket.value && webSocket.value.readyState !== WebSocket.OPEN) {
        console.log(`檢測到WebSocket狀態異常 (readyState: ${webSocket.value.readyState})，嘗試重新連接`);
        initWebSocket();
        return;
    }
    
    // 如果連接正常，發送ping確保活躍
    if (isConnected.value && webSocket.value && webSocket.value.readyState === WebSocket.OPEN) {
        webSocket.value.send(JSON.stringify({ cmd: 'ping' }));
    }
};

// 在onMounted中設置定期健康檢查
onMounted(() => {
    // 其他初始化...
    refreshStatus();
    listConnections();
    fetchPredefinedScripts();
    
    // 設置連接健康檢查，每30秒檢查一次
    const healthCheckInterval = setInterval(checkConnectionHealth, 30000);
    
    // 添加窗口消息監聽器，用於處理iframe的消息
    window.addEventListener('message', (event) => {
        // 確保消息來源是我們的iframe
        const iframe = document.querySelector('.display-iframe');
        if (iframe && event.source === iframe.contentWindow) {
            if (event.data.type === 'guacamole-event') {
                // 處理來自iframe的事件
                console.log('Received event from iframe:', event.data);
                if (event.data.event === 'keydown' || event.data.event === 'keyup') {
                    // 處理鍵盤事件
                    const keysym = event.data.keysym;
                    const state = event.data.event === 'keydown' ? '1' : '0';
                    executeCommand(`key ${keysym} ${state}`);
                } else if (event.data.event === 'mousemove' || event.data.event === 'mousedown' || event.data.event === 'mouseup') {
                    // 處理鼠標事件
                    const x = event.data.x;
                    const y = event.data.y;
                    const button = event.data.button || 0;
                    const action = event.data.event.replace('mouse', '');
                    executeCommand(`mouse ${x} ${y} ${button} ${action}`);
                }
            }
        }
    });
    
    // 在組件卸載時清除定時器和事件監聽器
    onUnmounted(() => {
        clearInterval(healthCheckInterval);
        if (webSocket.value) {
            webSocket.value.close();
        }
        window.removeEventListener('message', () => {});
    });
});


</script>

<template>
<div class="caldera-guacamole-plugin">
  <h2 class="plugin-title">Caldera Guacamole 插件</h2>
  
  <div class="main-container">
    <div class="panels-container">
      <div class="left-panel">
        <section class="service-status panel">
          <h3>服務狀態</h3>
          <div class="status-grid">
            <div class="status-item">
              <span class="status-label">guacd:</span>
              <span :class="status.guacd === 'running' ? 'status-running' : 'status-stopped'">{{ status.guacd }}</span>
            </div>
            <div class="status-item">
              <span class="status-label">guacamole:</span>
              <span :class="status.guacamole === 'running' ? 'status-running' : 'status-stopped'">{{ status.guacamole }}</span>
            </div>
            <div class="status-item">
              <span class="status-label">mysql:</span>
              <span :class="status.mysql === 'running' ? 'status-running' : 'status-stopped'">{{ status.mysql }}</span>
            </div>
          </div>

          <div class="button-group">
            <button @click="startService" :disabled="isLoading" class="primary-button">啟動服務</button>
            <button @click="stopService" :disabled="isLoading" class="danger-button">停止服務</button>
            <button @click="refreshStatus" :disabled="isLoading" class="secondary-button">刷新狀態</button>
          </div>
        </section>

        <section class="guacamole-actions panel">
          <h3>Guacamole 操作</h3>
          <button @click="openGuacamole" :disabled="status.guacamole !== 'running'" class="primary-button full-width">
            打開 Guacamole 界面
          </button>
          <p v-if="status.guacamole !== 'running'" class="warning">請先啟動 Guacamole 服務</p>
        </section>

        <section class="debug-info panel">
          <h3>調試信息</h3>
          <pre>{{ debugInfo }}</pre>
        </section>
      </div>
      
      <div class="right-panel">
        <section class="new-connection panel">
          <h3>創建新連接</h3>
          <div v-if="connectionCreated" class="success-message">連接創建成功！</div>
          <form @submit.prevent="createConnection">
            <div class="form-row">
              <div class="form-group">
                <label for="connection-name">連接名稱</label>
                <input type="text" id="connection-name" v-model="newConnection.name" placeholder="例如：我的 RDP 連接" required />
              </div>
              <div class="form-group">
                <label for="protocol">協議</label>
                <select id="protocol" v-model="newConnection.protocol">
                  <option value="RDP">RDP</option>
                  <option value="SSH">SSH</option>
                  <option value="VNC">VNC</option>
                </select>
              </div>
            </div>

            <div class="form-row">
              <div class="form-group">
                <label for="host-address">主機地址</label>
                <input type="text" id="host-address" v-model="newConnection.host" placeholder="例如：192.168.1.100" required />
              </div>
              <div class="form-group">
                <label for="port">端口</label>
                <input type="number" id="port" v-model.number="newConnection.port" required />
              </div>
            </div>

            <div class="form-row">
              <div class="form-group">
                <label for="username">用戶名</label>
                <input type="text" id="username" v-model="newConnection.username" placeholder="例如：administrator" required />
              </div>
              <div class="form-group">
                <label for="password">密碼</label>
                <input type="password" id="password" v-model="newConnection.password" required />
              </div>
            </div>

            <button type="submit" :disabled="isLoading || status.guacamole !== 'running'" class="primary-button full-width">創建連接</button>
          </form>
        </section>

        <section class="connection-list panel">
          <div class="section-header">
            <h3>現有連接</h3>
            <button @click="listConnections" :disabled="isLoading || status.guacamole !== 'running'" class="secondary-button">
              刷新連接列表
            </button>
          </div>
          
          <div v-if="!connections || connections.length === 0" class="no-connections">
            尚無連接。請創建新連接。
          </div>
          <table v-else class="connections-table">
            <thead>
              <tr>
                <th>名稱</th>
                <th>協議</th>
                <th>主機</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="conn in connections" :key="conn.identifier">
                <td>{{ conn.name || 'Unknown' }}</td>
                <td>{{ conn.protocol ? conn.protocol.toUpperCase() : 'Unknown' }}</td>
                <td>{{ conn.parameters?.hostname || 'Unknown' }}</td>
                <td>
                  <button @click="openConnection(conn)" class="small-button">打開</button>
                </td>
              </tr>
            </tbody>
          </table>
        </section>
      </div>
    </div>
    
<!-- 遠程會話區域 -->
<section v-if="activeConnections.length > 0" class="remote-session panel">
  <div class="tab-header">
    <div 
      v-for="(conn, index) in activeConnections" 
      :key="conn.identifier"
      :class="['tab', { active: index === activeTabIndex }]"
      @click="switchTab(index)"
    >
      <span>{{ conn.name }}</span>
      <button @click.stop="closeTab(index)" class="tab-close">×</button>
    </div>
  </div>
  
  <!-- 根據連接類型顯示不同的內容 -->
  <div class="connection-content">
    <div class="remote-display">
      <a :href="`/plugin/guacamole/display?id=${activeConnections[activeTabIndex]?.identifier}`" 
         target="_blank" 
         class="open-display-link">
        在新窗口中打開遠程桌面
      </a>
      <iframe 
        :src="`/plugin/guacamole/display?id=${activeConnections[activeTabIndex]?.identifier}&embedded=true`" 
        frameborder="0" 
        allowfullscreen
        class="display-iframe"
        sandbox="allow-same-origin allow-scripts allow-forms"
      ></iframe>
    </div>
  </div>
</section>

    
    
    
    <!-- 腳本控制面板 -->
    <section v-if="activeConnections.length > 0" class="script-control panel">
      <h3>腳本控制面板</h3>
      
      <div class="script-editor">
        <div class="editor-toolbar">
          <button @click="executeScript" :disabled="!scriptContent" class="primary-button">執行腳本</button>
          <button @click="clearScript" class="secondary-button">清空</button>
          <button @click="saveScript" class="secondary-button">保存</button>
          <button @click="loadScript" class="secondary-button">載入</button>
          <button @click="startRecording" :class="isRecording ? 'danger-button' : 'secondary-button'">
            {{ isRecording ? '停止錄製' : '開始錄製' }}
          </button>
          <button @click="replayRecording" :disabled="recordedCommands.length === 0" class="secondary-button">重放</button>
        </div>
        
        <textarea 
          v-model="scriptContent" 
          placeholder="輸入腳本命令，一行一條。例如：
mouse 100 100 1 click
type Hello World
wait 1
key ctrl 1
key a 1
key a 0
key ctrl 0" 
          class="script-textarea"
        ></textarea>
      </div>
      
      <div class="quick-commands">
        <h4>快速命令</h4>
        <div class="command-grid">
          <button v-for="cmd in quickCommands" :key="cmd.id" 
                  @click="addCommand(cmd.command)" 
                  class="command-button">
            <span class="command-icon" :class="cmd.icon"></span>
            <span class="command-name">{{ cmd.name }}</span>
          </button>
        </div>
      </div>
      
      <div class="predefined-scripts">
        <h4>預定義腳本</h4>
        <div class="scripts-grid">
          <button v-for="script in predefinedScripts" :key="script.id" 
                  @click="runPredefinedScript(script.id)" 
                  class="script-button"
                  :title="script.description">
            <span class="script-icon" :class="script.icon"></span>
            <span class="script-name">{{ script.name }}</span>
          </button>
        </div>
      </div>
      
      <div class="command-output">
        <h4>命令輸出</h4>
        <div class="output-container" ref="outputContainer">
          <div v-for="(line, index) in commandOutput" :key="index" 
               :class="['output-line', line.type]">
            {{ line.text }}
          </div>
        </div>
        <div class="output-toolbar">
          <button @click="clearOutput" class="secondary-button">清空輸出</button>
        </div>
      </div>
      
      <div class="realtime-controls">
        <h4>即時控制</h4>
        <div class="mouse-control">
          <div class="mouse-pad" 
               @mousemove="onMousePadMove" 
               @mousedown="onMousePadDown"
               @mouseup="onMousePadUp"
               @mouseleave="onMousePadLeave"
               ref="mousePad">
            <div class="mouse-cursor" :style="mouseCursorStyle"></div>
          </div>
          <div class="mouse-buttons">
            <button @mousedown="onMouseButtonEvent(1, 'down')" 
                    @mouseup="onMouseButtonEvent(1, 'up')"
                    class="mouse-button">左鍵</button>
            <button @mousedown="onMouseButtonEvent(2, 'down')" 
                    @mouseup="onMouseButtonEvent(2, 'up')"
                    class="mouse-button">中鍵</button>
            <button @mousedown="onMouseButtonEvent(3, 'down')" 
                    @mouseup="onMouseButtonEvent(3, 'up')"
                    class="mouse-button">右鍵</button>
          </div>
        </div>
        
        <div class="keyboard-control">
          <input type="text" v-model="keyboardInput" placeholder="輸入文字後按發送" />
          <button @click="sendKeyboardInput" class="primary-button">發送</button>
          <div class="special-keys">
            <button v-for="key in specialKeys" :key="key.code" 
                    @click="sendSpecialKey(key.code)" 
                    class="special-key">{{ key.name }}</button>
          </div>
        </div>
      </div>
    </section>
  </div>
</div>
</template>

<style scoped>
.caldera-guacamole-plugin {
  padding: 20px;
  font-family: Arial, sans-serif;
  color: #333;
}

.plugin-title {
  text-align: center;
  margin-bottom: 20px;
  color: #2c3e50;
  padding-bottom: 10px;
  border-bottom: 2px solid #eee;
}

.main-container {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.panels-container {
  display: flex;
  gap: 20px;
  flex-wrap: wrap; /* 允許在小屏幕上換行 */
}

.left-panel, .right-panel {
  flex: 1;
  min-width: 300px; /* 確保最小寬度 */
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.panel {
  background-color: #fff;
  border-radius: 8px;
  padding: 15px;
  box-shadow: 0 2px 4px rgba(0,0,0,0.1);
}

.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 15px;
}

h3 {
  margin-top: 0;
  margin-bottom: 15px;
  color: #2c3e50;
  border-bottom: 1px solid #eee;
  padding-bottom: 8px;
}

h4 {
  margin-top: 15px;
  margin-bottom: 10px;
  color: #2c3e50;
}

.status-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 10px;
  margin-bottom: 15px;
}

.status-item {
  display: flex;
  align-items: center;
}

.status-label {
  font-weight: bold;
  margin-right: 10px;
  width: 100px;
}

.status-running {
  color: #27ae60;
  font-weight: bold;
}

.status-stopped {
  color: #e74c3c;
}

.button-group {
  display: flex;
  gap: 10px;
  margin-top: 15px;
}

button {
  padding: 8px 12px;
  border-radius: 4px;
  border: none;
  cursor: pointer;
  font-weight: bold;
  transition: background-color 0.2s;
}

.primary-button {
  background-color: #3498db;
  color: white;
}

.primary-button:hover {
  background-color: #2980b9;
}

.danger-button {
  background-color: #e74c3c;
  color: white;
}

.danger-button:hover {
  background-color: #c0392b;
}

.secondary-button {
  background-color: #95a5a6;
  color: white;
}

.secondary-button:hover {
  background-color: #7f8c8d;
}

.small-button {
  padding: 4px 8px;
  font-size: 12px;
  background-color: #3498db;
  color: white;
}

button:disabled {
  background-color: #bdc3c7;
  cursor: not-allowed;
}

.full-width {
  width: 100%;
}

.form-row {
  display: flex;
  gap: 15px;
  margin-bottom: 15px;
}

.form-group {
  flex: 1;
}

label {
  display: block;
  margin-bottom: 5px;
  font-weight: bold;
  color: #555;
}

input, select, textarea {
  width: 100%;
  padding: 8px;
  border-radius: 4px;
  border: 1px solid #ddd;
  box-sizing: border-box;
}

input:focus, select:focus, textarea:focus {
  border-color: #3498db;
  outline: none;
}

.warning {
  color: #e67e22;
  margin-top: 10px;
  font-style: italic;
}

.success-message {
  background-color: #d4edda;
  color: #155724;
  padding: 10px;
  border-radius: 4px;
  margin-bottom: 15px;
  text-align: center;
}

pre {
  background-color: #f8f9fa;
  padding: 10px;
  border-radius: 4px;
  overflow: auto;
  font-size: 12px;
  max-height: 200px;
}

.connections-table {
  width: 100%;
  border-collapse: collapse;
  margin-top: 10px;
  font-size: 14px;
}

.connections-table th, .connections-table td {
  border: 1px solid #ddd;
  padding: 8px;
  text-align: left;
}

.connections-table th {
  background-color: #f2f2f2;
  font-weight: bold;
}

.connections-table tr:nth-child(even) {
  background-color: #f9f9f9;
}

.connections-table tr:hover {
  background-color: #f5f5f5;
}

.no-connections {
  text-align: center;
  padding: 20px;
  color: #7f8c8d;
  font-style: italic;
}

/* 修正問題1：調整遠程會話區域的CSS */
.remote-session {
  display: flex;
  flex-direction: column;
  height: auto;
  min-height: 820px; /* 增加最小高度 */
  margin-bottom: 20px;
  width: 100%;
}

.tab-header {
  display: flex;
  justify-content: flex-start;
  background-color: #f0f0f0;
  padding: 10px;
  flex-wrap: nowrap;
  overflow-x: auto;
}

.tab {
  display: flex;
  align-items: center;
  padding: 8px 15px;
  background-color: #e0e0e0;
  border-top-left-radius: 5px;
  border-top-right-radius: 5px;
  margin: 5px 5px 0 0;
}

.tab.active {
  background-color: #fff;
  border-bottom: none;
}

.tab-close {
  margin-left: 10px;
  background: none;
  border: none;
  font-size: 16px;
  cursor: pointer;
  color: #555;
}

.tab-close:hover {
  color: #e74c3c;
}

.connection-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 修正問題1：調整遠程顯示區域的CSS */
.remote-display {
  width: 100%;
  height: 100%;
  overflow: auto;
  background-color: #282c34;
  padding: 10px;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.display-iframe {
  width: 100%;
  height: 768px;
  max-width: 1080px;
  border: none;
  background-color: #000;
  margin: 0 auto;
  display: block;
}

.open-display-link {
  display: block;
  padding: 10px;
  text-align: center;
  background-color: #f8f9fa;
  color: #3498db;
  text-decoration: none;
  margin-bottom: 10px;
  border-radius: 4px;
  width: 100%;
  max-width: 1080px;
}

.open-display-link:hover {
  background-color: #e9ecef;
}

#display {
  width: 100%;
  height: 100%;
  display: block;
}

#mouseCursor {
  position: absolute;
  width: 20px;
  height: 20px;
  background-image: url('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAAXNSR0IArs4c6QAAACBjSFJNAAB6JgAAgIQAAPoAAACA6AAAdTAAAOpgAAA6mAAAF3CculE8AAAAAmJLR0QA/4ePzL8AAAAJcEhZcwAACxMAAAsTAQCanBgAAAAHdElNRQfhCAoTKw0L12RjAAABFklEQVQ4y63TsUtCURjG8d+xjZAPkbEIU0SRqYNLg4NBi6FDt4P/gEOTQ5uDS5tCk5vA4CAqImL4pEAxUYgIFKKYKFVUxBUVPyL4fl4Tz5w53BvO3XPO4RxnHGfNzrk5366GPYUu3jLNIx7xcY8N5vl/tStYxiROcX4VGeMMV5jGVLaxjP+MaS0Lq9jGN04q3sEdNnGJEUxrRjGNadxiGTc4w3p9pBjfOYkNPGMSc5jGEq7xjU2M4lHGNK4xiz00Z5TQCvawh028YUhjK/s4wSYmcYITnOAFe9jCGI4wjw284wSnWMYt5nCDJRzhEic4w1mcY02f4QqLOEaNn3/ET3zFKT5hEnP4xZk28YwJLuEedzjbKz7iHc4wwcEFLpMAAAAASUVORK5CYII=');
  background-repeat: no-repeat;
  background-size: contain;
  pointer-events: none;
  transform: translate(-50%, -50%);
  z-index: 10;
}

/* 腳本控制面板樣式 */
.script-control {
  margin-top: 20px;
  width: 100%;
}

.script-editor {
  margin-bottom: 15px;
}

.editor-toolbar {
  display: flex;
  gap: 10px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}

.script-textarea {
  width: 100%;
  height: 120px;
  padding: 10px;
  font-family: monospace;
  border: 1px solid #ddd;
  border-radius: 4px;
  resize: vertical;
}

.quick-commands, .predefined-scripts {
  margin-bottom: 15px;
}

.command-grid, .scripts-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 10px;
  margin-top: 10px;
}

.command-button, .script-button {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 10px;
  background-color: #f8f9fa;
  border: 1px solid #ddd;
  border-radius: 4px;
  cursor: pointer;
  transition: background-color 0.2s;
}

.command-button:hover, .script-button:hover {
  background-color: #e9ecef;
}

.command-icon, .script-icon {
  font-size: 24px;
  margin-bottom: 5px;
}

.command-name, .script-name {
  font-size: 12px;
  text-align: center;
}

.command-output {
  margin-bottom: 15px;
}

.output-container {
  height: 150px;
  padding: 10px;
  background-color: #2d3436;
  color: #dfe6e9;
  font-family: monospace;
  border-radius: 4px;
  overflow-y: auto;
}

.output-line {
  margin-bottom: 3px;
  white-space: pre-wrap;
  word-break: break-word;
}

.output-line.info {
  color: #74b9ff;
}

.output-line.success {
  color: #55efc4;
}

.output-line.error {
  color: #ff7675;
}

.output-line.command {
  color: #ffeaa7;
  font-weight: bold;
}

.output-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-top: 5px;
}

.realtime-controls {
  margin-bottom: 15px;
}

.mouse-control {
  display: flex;
  flex-direction: column;
  margin-bottom: 15px;
}

.mouse-pad {
  width: 100%;
  height: 200px;
  background-color: #f8f9fa;
  border: 1px solid #ddd;
  border-radius: 4px;
  position: relative;
  cursor: crosshair;
  margin-bottom: 10px;
}

.mouse-cursor {
  width: 10px;
  height: 10px;
  background-color: red;
  border-radius: 50%;
  position: absolute;
  transform: translate(-50%, -50%);
  pointer-events: none;
}

.mouse-buttons {
  display: flex;
  gap: 10px;
}

.mouse-button {
  flex: 1;
  padding: 8px;
  background-color: #f8f9fa;
  border: 1px solid #ddd;
  border-radius: 4px;
  cursor: pointer;
}

.keyboard-control {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.keyboard-control input {
  padding: 8px;
  border: 1px solid #ddd;
  border-radius: 4px;
}

.special-keys {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 5px;
  margin-top: 10px;
}

.special-key {
  padding: 8px;
  background-color: #f8f9fa;
  border: 1px solid #ddd;
  border-radius: 4px;
  cursor: pointer;
  text-align: center;
}

/* 響應式設計 */
@media (max-width: 1200px) {
  .special-keys {
    grid-template-columns: repeat(4, 1fr);
  }
  
  .display-iframe {
    height: 600px;
  }
}

@media (max-width: 768px) {
  .panels-container {
    flex-direction: column;
  }
  
  .form-row {
    flex-direction: column;
    gap: 10px;
  }
  
  .special-keys {
    grid-template-columns: repeat(3, 1fr);
  }
  
  .display-iframe {
    height: 480px;
  }
}

/* 大屏幕優化 */
@media (min-width: 1600px) {
  .display-iframe {
    height: 900px;
    max-width: 1280px;
  }
  
  .open-display-link {
    max-width: 1280px;
  }
  
  .remote-session {
    min-height: 950px;
  }
}
</style>
