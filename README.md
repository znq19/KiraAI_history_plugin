# KiraAI_history_plugin/跨会话历史消息与总结插件
提供工具让AI可以跨会话获取群聊/私聊消息历史 Provide tools to fetch group/private message history

0.插件功能：可以让你的ai跨会话（群聊和私聊）总结目标群和私聊的聊天记录。必须是同适配器和已建立会话的目标。
基于onebot的http服务。
更新1.20版，可提供正确的消息id和图片消息真实url，一定程度上辅助kiraai2.0后的合并转发消息功能，并防止AI犯蠢一直重复读取。

1.安装方法：

（1）安装给kira项目文件夹安装依赖（请根据你的实际路径调整）：

cd C:\Users\Administrator\Desktop\KiraAI-main

venv\Scripts\activate

pip install httpx

（2）onebot程序开启HTTP服务，确认端口（默认3000）和token。

（3）复制history_plugin文件夹到\data\plugins。

（4）webui中配置主人账号。主人账号拥有所有群聊和私聊的总结权限。普通用户无法看到其他私聊和限制的群。

可选：

（5）安装https://github.com/LyaQanYi/KiraOS_Plugin?tab=readme-ov-file#kiraos，作为前置插件。这可使用一种总结skill，但不使用也没关系。

复制summarize_other文件夹到\data\skills（如没有修改kiraos的skills默认地址）。


2.使用方法：跟ai说“总结某某群/某某人（为确保精确，首次可以特别说明QQ号）最近聊了些什么”之类的话。

默认条数可在skills中配置，也可直接使用时跟ai说明。
