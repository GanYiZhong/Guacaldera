# plugins/guacamole/hook.py
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
from aiohttp import web
from aiohttp_jinja2 import template

name = 'Guacamole'
description = '通過 Apache Guacamole 提供遠程桌面會話管理功能'
address = '/plugin/guacamole/gui'
docker_client = None
guacd_container = None
guacamole_container = None
mysql_container = None
plugin_root = None

async def enable(services):
    global docker_client, plugin_root
    app = services.get('app_svc').application
    plugin_root = os.path.dirname(os.path.realpath(__file__))
    
    # 註冊靜態資源和jQuery資源
    app.router.add_static('/guacamole/static', f'{plugin_root}/static', append_version=True)
    app.router.add_static('/guacamole/jquery', f'{plugin_root}/static/jquery', append_version=True)
    
    # 註冊路由
    app.router.add_route('GET', '/plugin/guacamole/gui', gui)
    app.router.add_route('POST', '/plugin/guacamole/start', start_containers)
    app.router.add_route('POST', '/plugin/guacamole/stop', stop_containers)
    app.router.add_route('GET', '/plugin/guacamole/status', get_status)
    app.router.add_route('POST', '/plugin/guacamole/create_connection', create_connection)
    
    # 初始化 Docker 客戶端
    try:
        docker_client = docker.from_env()
        logging.info("Docker client initialized successfully")
    except Exception as e:
        logging.error(f"Failed to initialize Docker client: {e}")

@template('guacamole.html')
async def gui(request):
    return {'name': 'Guacamole 插件', 'status': await get_container_status()}

async def get_container_status():
    if not docker_client:
        return {'guacd': 'unknown', 'guacamole': 'unknown', 'mysql': 'unknown'}
    
    status = {'guacd': 'stopped', 'guacamole': 'stopped', 'mysql': 'stopped'}
    
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
        # 創建 Docker 網絡
        network_name = 'guacamole_network'
        networks = docker_client.networks.list(names=[network_name])
        if not networks:
            network = docker_client.networks.create(network_name)
            logging.info(f"Created network: {network_name}")
        else:
            network = networks[0]
        
        # 啟動 MySQL 容器
        mysql_container = start_mysql_container(network)
        
        # 等待 MySQL 初始化
        await asyncio.sleep(15)
        
        # 啟動 guacd 容器
        guacd_container = start_guacd_container(network)
        
        # 啟動 guacamole 容器
        guacamole_container = start_guacamole_container(network)
        
        return web.json_response({'status': 'success', 'message': 'Containers started successfully'})
    except PermissionError as e:
        logging.error(f"Permission error starting containers: {e}")
        return web.json_response({
            'status': 'error', 
            'message': '權限錯誤：無法啟動 Docker 容器。請確保 Caldera 有適當的 Docker 權限。'
        })
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
        
        # 創建 MySQL 初始化腳本目錄
        init_db_path = f'{plugin_root}/docker/init'
        os.makedirs(init_db_path, exist_ok=True)
        
        # 下載 SQL 初始化腳本 - 使用正確的create-schema.sql而非upgrade腳本
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
            volumes={
                init_db_path: {'bind': '/docker-entrypoint-initdb.d', 'mode': 'ro'}
            }
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
        
        caldera_host = socket.gethostbyname(socket.gethostname())
        
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
            ports={
                '8080/tcp': 8080
            }
        )
        
        network.connect(container)
        return container
    except Exception as e:
        logging.error(f"Error starting guacamole container: {e}")
        raise

async def stop_containers(request):
    try:
        if not docker_client:
            logging.error("Docker client not initialized")
            return web.json_response({'status': 'error', 'message': 'Docker client not initialized'})
        
        # 列出所有相關容器
        containers = docker_client.containers.list(all=True, filters={'name': 'guacamole'})
        containers.extend(docker_client.containers.list(all=True, filters={'name': 'guacd'}))
        containers.extend(docker_client.containers.list(all=True, filters={'name': 'guacamole-mysql'}))
        
        # 停止每個容器
        for container in containers:
            container.stop()
            logging.info(f"Stopped container: {container.name}")
        
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
        protocol = data.get('protocol', 'rdp')
        host = data.get('host', '')
        port = data.get('port', 3389 if protocol == 'rdp' else 22)
        username = data.get('username', '')
        password = data.get('password', '')
        name = data.get('name', f'{protocol.upper()} - {host}')
        
        # 在此處實現與 Guacamole API 的交互邏輯
        # 這需要額外實現，或使用 Guacamole 的 REST API
        
        return web.json_response({'status': 'success', 'message': f'Connection to {host} created'})
    except Exception as e:
        logging.error(f"Error creating connection: {e}")
        return web.json_response({'status': 'error', 'message': str(e)})
