# Guacaldera

Guacaldera 是一個基於 Apache Guacamole 的遠程桌面管理插件，提供了便捷的遠程桌面會話管理功能。它整合了 Docker 容器化部署，支援多種遠程協議（RDP、SSH、VNC），並提供了直觀的網頁界面。

## 功能特點

- 🖥️ 支援多種遠程協議：
  - RDP (Remote Desktop Protocol)
  - SSH (Secure Shell)
  - VNC (Virtual Network Computing)
- 🐳 Docker 容器化部署
- 🔐 安全的連接管理
- 🎮 實時控制功能：
  - 鍵盤輸入
  - 滑鼠控制
  - 特殊按鍵支援
- 📝 腳本自動化支援
- 🖼️ 多會話管理
- 🔄 自動重連機制

## 系統要求

- Docker Engine 20.10.0 或更高版本
- 作業系統：
  - Windows 10/11
  - Linux (Ubuntu 20.04+, CentOS 8+)
  - macOS 10.15+
- 網頁瀏覽器：
  - Chrome 90+
  - Firefox 88+
  - Safari 14+
  - Edge 90+

## 快速開始

### 1. 安裝

```bash
# 克隆專案
git clone https://github.com/yourusername/Guacaldera.git
cd Guacaldera

# 建立必要的目錄
mkdir -p docker/init
```

### 2. 配置

預設配置已經包含在專案中，但您可以根據需要修改以下設定：

- Guacamole 服務端口：8080
- MySQL 資料庫設定：
  - 資料庫：guacamole_db
  - 用戶名：guacamole_user
  - 密碼：guacamole_pass

### 3. 啟動服務

通過網頁界面：
1. 訪問插件界面
2. 點擊「啟動服務」按鈕
3. 等待所有容器啟動完成

或通過 API：
```bash
curl -X POST http://localhost:8080/plugin/guacamole/start
```

### 4. 創建連接

1. 在網頁界面中點擊「新建連接」
2. 選擇協議類型（RDP/SSH/VNC）
3. 填寫連接資訊：
   - 名稱
   - 主機地址
   - 端口
   - 用戶名
   - 密碼
4. 點擊「創建」完成設置

## API 參考

### 基礎端點

- `GET /plugin/guacamole/gui` - 網頁界面
- `POST /plugin/guacamole/start` - 啟動服務
- `POST /plugin/guacamole/stop` - 停止服務
- `GET /plugin/guacamole/status` - 獲取服務狀態

### 連接管理

- `POST /plugin/guacamole/create_connection` - 創建新連接
- `GET /plugin/guacamole/list_connections` - 列出所有連接
- `POST /plugin/guacamole/execute_command` - 執行遠程命令
- `POST /plugin/guacamole/execute_script` - 執行自動化腳本

## 安全性考慮

1. 預設憑證
   - 請務必更改預設的管理員密碼
   - 建議使用環境變數或配置文件管理敏感資訊

2. 網絡安全
   - 建議在反向代理後使用
   - 啟用 SSL/TLS 加密
   - 限制訪問 IP

3. 容器安全
   - 定期更新容器鏡像
   - 使用非 root 用戶運行容器
   - 限制容器資源使用

## 故障排除

### 常見問題

1. 服務無法啟動
   - 檢查 Docker 服務狀態
   - 確認端口是否被占用
   - 查看日誌文件

2. 連接失敗
   - 確認網絡連通性
   - 檢查目標主機防火牆設置
   - 驗證認證信息

3. 性能問題
   - 調整容器資源限制
   - 檢查網絡帶寬
   - 優化圖像質量設置

## 貢獻指南

我們歡迎任何形式的貢獻，包括但不限於：

- 🐛 Bug 報告和修復
- ✨ 新功能建議和實現
- 📝 文檔改進
- 🌐 多語言支援

請遵循以下步驟：

1. Fork 專案
2. 創建特性分支
3. 提交更改
4. 推送到分支
5. 創建 Pull Request

## 授權協議

本專案採用 Apache License 2.0 授權協議。詳見 [LICENSE](LICENSE) 文件。

## 致謝

- [Apache Guacamole](https://guacamole.apache.org/)
- [Docker](https://www.docker.com/)
- 所有貢獻者

## 聯繫方式

- 作者：[您的名字]
- Email：[您的郵箱]
- GitHub：[您的 GitHub 主頁] 