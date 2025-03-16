<script setup>
import { ref, onMounted } from "vue";
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
  port: null,
  username: '',
  password: ''
});

// 確保 debugInfo 只宣告一次
const debugInfo = ref('');

const refreshStatus = async () => {
  try {
    const { data } = await axios.get('/plugin/guacamole/status');
    status.value = data;
  } catch (error) {
    debugInfo.value = `刷新狀態失敗: ${error}`;
    console.error(error);
  }
};

const startService = async () => {
  try {
    const response = await axios.post('/plugin/guacamole/start');
    const data = response.data; // 確保正確解析響應
    debugInfo.value = `啟動成功：${data.message}`;
    await refreshStatus();
  } catch (error) {
    if (error.response) {
      // 後端返回錯誤響應
      debugInfo.value = `啟動服務失敗：${error.response.data.message}`;
    } else if (error.request) {
      // 請求已發出但未收到響應
      debugInfo.value = '啟動服務失敗：未收到後端響應';
    } else {
      // 其他錯誤
      debugInfo.value = `啟動服務失敗：${error.message}`;
    }
    console.error(error);
  }
};


const stopService = async () => {
  try {
    await axios.post('/plugin/guacamole/stop');
    await refreshStatus();
  } catch (error) {
    debugInfo.value = `停止服務失敗：${error}`;
    console.error(error);
  }
};

const openGuacamole = () => {
  window.open('/guacamole');
};

const createConnection = async () => {
  try {
    const res = await axios.post('/plugin/guacamole/create_connection', newConnection.value);
    debugInfo.value = JSON.stringify(res.data, null, 2);
  } catch (error) {
    debugInfo.value = `建立連接失敗：${error}`;
    console.error(error);
  }
};

onMounted(() => {
  refreshStatus();
});
</script>

<template>
<div class="caldera-guacamole-plugin">
  <h2>Caldera Guacamole 插件</h2>

  <section class="service-status">
    <h3>服務狀態</h3>
    <ul>
      <li>guacd: {{ status.guacd }}</li>
      <li>guacamole: {{ status.guacamole }}</li>
      <li>mysql: {{ status.mysql }}</li>
    </ul>

    <button @click="startService">啟動服務</button>
    <button @click="stopService">停止服務</button>
    <button @click="refreshStatus">刷新狀態</button>
  </section>

  <section class="guacamole-actions">
    <button @click="openGuacamole">打開 Guacamole</button>
  </section>

  <section class="new-connection">
    <h3>創建新連接</h3>
    <form @submit.prevent="createConnection">
      <label for="connection-name">連接名稱</label>
      <input type="text" id="connection-name" v-model="newConnection.name" required />

      <label for="protocol">協議</label>
      <select id="protocol" v-model="newConnection.protocol">
        <option value="RDP">RDP</option>
        <option value="SSH">SSH</option>
        <option value="VNC">VNC</option>
      </select>

      <label for="host-address">主機地址</label>
      <input type="text" id="host-address" v-model="newConnection.host" required />

      <label for="port">端口</label>
      <input type="number" id="port" v-model.number="newConnection.port" required />

      <label for="username">用戶名</label>
      <input type="text" id="username" v-model="newConnection.username" required />

      <label for="password">密碼</label>
      <input type="password" id="password" v-model="newConnection.password" required />

      <button type="submit">創建連接</button>
    </form>
  </section>

  <section class="debug-info">
    <h3>調試信息</h3>
    <pre>{{ debugInfo }}</pre>
  </section>
</div>
</template>

<style scoped>
.caldera-guacamole-plugin { padding:20px; }
.service-status, .new-connection, .debug-info, .guacamole-actions { margin-bottom:20px; }
label { display:block; margin-top:10px; }
input, select, button { margin-top:5px; }
button { cursor:pointer; }
pre { background-color:#f0f0f0; padding:10px; }
</style>
