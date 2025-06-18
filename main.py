from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import Node, Plain, Image
import aiohttp
import asyncio
import re
import os
import ssl
import yt_dlp
import certifi
import pdfplumber
import tomli
import time
from typing import Dict, Optional, TYPE_CHECKING
import json
import html
import xml.etree.ElementTree as ET

@register("summary", "whyis", "一个简单的读取网页内容总结", "1.0.0")
class MyPlugin(Star):
    URL_PATTERN = r'https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+[-\w./?=&]*'

    def __init__(self, context: Context):
        super().__init__(context)
        self.name = "Summary"
        config_path = os.path.join(os.path.dirname(__file__), "config.toml")
        with open(config_path, "rb") as f:
            config = tomli.load(f)

        self.config = config.get("Summary", {})
        dify_config = self.config.get("Dify", {})
        self.dify_enable = dify_config.get("enable", False)
        self.dify_api_key = dify_config.get("api-key", "")
        self.dify_base_url = dify_config.get("base-url", "")
        self.http_proxy = dify_config.get("http-proxy", "")

        settings = self.config.get("Settings", {})
        self.max_text_length = settings.get("max_text_length", 10000)
        self.black_list = settings.get("black_list", [])
        self.white_list = settings.get("white_list", [])

        # 存储最近的链接和卡片信息
        self.recent_urls = {}  # 格式: {chat_id: {"url": url, "timestamp": timestamp}}
        self.recent_cards = {}  # 格式: {chat_id: {"info": card_info, "timestamp": timestamp}}
        # 链接和卡片的过期时间（秒）
        self.expiration_time = 300  # 5分钟

        self.http_session = aiohttp.ClientSession()

        if not self.dify_enable or not self.dify_api_key or not self.dify_base_url:
            logger.warning("Dify配置不完整，自动总结功能将被禁用")
            self.dify_enable = False

    async def close(self):
        if self.http_session:
            await self.http_session.close()
            logger.info("HTTP会话已关闭")

    def _check_url(self, url: str) -> bool:
        stripped_url = url.strip()
        if not stripped_url.startswith(('http://', 'https://')):
            return False
        if self.white_list and not any(stripped_url.startswith(white_url) for white_url in self.white_list):
            return False
        if any(stripped_url.startswith(black_url) for black_url in self.black_list):
            return False
        return True

    def _clean_expired_items(self):
        current_time = time.time()
        # 清理过期的URL
        for chat_id in list(self.recent_urls.keys()):
            if current_time - self.recent_urls[chat_id]["timestamp"] > self.expiration_time:
                del self.recent_urls[chat_id]
        # 清理过期的卡片
        for chat_id in list(self.recent_cards.keys()):
            if current_time - self.recent_cards[chat_id]["timestamp"] > self.expiration_time:
                del self.recent_cards[chat_id]

    async def get_arxiv_paper_text(self, arxiv_url, http_session=None):
        # 步骤 1：从 URL 中提取论文 ID
        paper_id = arxiv_url.split('/')[-1]
        pdf_url = f"https://arxiv.org/pdf/{paper_id}.pdf"
        logger.info(f"下载pdf文件: {pdf_url}")
        if http_session is None:
            http_session = aiohttp.ClientSession()
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            async with http_session.get(pdf_url, timeout=timeout, ssl=ssl_context) as response:
                if response.status == 200:
                    # 获取二进制内容
                    pdf_content = await response.read()
                    logger.info(f"成功下载到pdf: {pdf_url}, size: {len(pdf_content)} bytes")
                else:
                    logger.error(f"无法下载pdf, status code: {response.status}")
                    return None
            await http_session.close()
        except Exception as e:
            logger.error(f"下载pdf时错误: {e}")
            return None
        finally:
            if http_session is None:
                await http_session.close()

        temp_pdf_path = f"{paper_id}.pdf"
        try:
            with open(temp_pdf_path, 'wb') as f:
                f.write(pdf_content)
        except Exception as e:
            logger.error(f"Error saving PDF to file: {e}")
            return None
        text_content = ""
        try:
            with pdfplumber.open(temp_pdf_path) as pdf:
                for page in pdf.pages:
                    text_content += page.extract_text() + "\n\n"
            logger.info(f"pdf文章处理完毕")
        except Exception as e:
            logger.error(f"pdf文章处理失败：{e}")
            text_content = "Could not extract text from PDF."

        if os.path.exists(temp_pdf_path):
            try:
                os.remove(temp_pdf_path)
            except Exception as e:
                logger.warning(f"删除临时文件错误: {e}")
        return text_content

    async def get_videos(self, video_url: str) -> Optional[str]:
        # 配置yt-dlp选项
        output_template = os.path.join('%(id)s.%(ext)s')
        ydl_opts = {
            'outtmpl': output_template,# 输出文件路径
            'format': 'bestvideo[height<=720]',
            'merge_output_format': 'mp4',  #合并为mp4格式
            'quiet': True,  #减少控制台输出
            'no_warnings': True,  # 忽略警告
            'ignoreerrors': False,
        }

        # 保存视频路径
        saved_path = None
        output_dir= None
        try:
            # 使用yt-dlp下载视频
            logger.info(f"开始下载视频: {video_url}")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info_dict = ydl.extract_info(video_url, download=True)
                video_id = info_dict.get('id', 'unknown')
                video_ext = info_dict.get('ext', 'mp4')
                saved_path = os.path.join(output_dir, f"{video_id}.{video_ext}")
        except Exception as e:
            logger.error(f"下载视频时出错: {e}")
            saved_path = None
        return saved_path

    async def get_github_code_text(self, github_url, http_session=None):
        pass
    async def _fetch_url_content(self, url: str) -> Optional[str]:
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            }
            # 不在顶层设置超时参数
            # 先检查是否有重定向，获取最终URL
            final_url = url
            try:
                # 只发送HEAD请求来检查重定向，不获取实际内容
                async def check_redirect():
                    # 在任务中设置超时
                    timeout = aiohttp.ClientTimeout(total=30)
                    async with self.http_session.head(url, headers=headers, allow_redirects=True,
                                                      timeout=timeout) as head_response:
                        if head_response.status == 200:
                            return str(head_response.url)
                        return url

                final_url = await asyncio.create_task(check_redirect())
                if final_url != url:
                    logger.info(f"检测到重定向: {url} -> {final_url}")
            except Exception as e:
                logger.warning(f"检查重定向失败: {e}, 使用原始URL")
                final_url = url

            # 使用 Jina AI 获取内容（使用最终URL）
            logger.info(f"使用 Jina AI 获取内容: {final_url}")
            try:
                jina_url = f"https://r.jina.ai/{final_url}"

                async def get_jina_content():
                    # 在任务中设置超时
                    timeout = aiohttp.ClientTimeout(total=30)
                    async with self.http_session.get(jina_url, headers=headers, timeout=timeout) as jina_response:
                        if jina_response.status == 200:
                            content = await jina_response.text()
                            return content
                        return None

                content = await asyncio.create_task(get_jina_content())
                if content:
                    logger.info(f"从 Jina AI 获取内容成功: {jina_url}, 内容长度: {len(content)}")
                    return content
                else:
                    logger.error(f"从 Jina AI 获取内容失败，URL: {jina_url}")
            except Exception as e:
                logger.error(f"使用Jina AI获取内容失败: {e}")

            # 如果 Jina AI 失败，尝试直接获取
            logger.info(f"Jina AI 失败，尝试直接获取: {final_url}")
            try:
                async def get_direct_content():
                    # 在任务中设置超时
                    timeout = aiohttp.ClientTimeout(total=30)
                    async with self.http_session.get(final_url, headers=headers, timeout=timeout) as response:
                        if response.status != 200:
                            logger.error(f"直接获取URL失败: {response.status}, URL: {final_url}")
                            return None

                        return await response.text()

                content = await asyncio.create_task(get_direct_content())
                if content and len(content) > 500:  # 确保内容有足够长度
                    logger.info(f"直接从URL获取内容成功: {final_url}, 内容长度: {len(content)}")
                    return content
            except Exception as e:
                logger.warning(f"直接获取内容失败: {e}")

            # 尝试使用备用方法直接获取
            return await self._fetch_url_content_direct(final_url)
        except asyncio.TimeoutError:
            logger.error(f"获取URL内容超时: URL: {url}")
            return None
        except Exception as e:
            logger.error(f"获取URL内容时出错: {e}, URL: {url}")
            return None

    async def _fetch_url_content_direct(self, url: str) -> Optional[str]:
        """直接获取URL内容的备用方法"""
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache"
            }
            # 不在顶层设置超时参数

            logger.info(f"备用方法尝试获取: {url}")

            async def get_backup_content():
                # 在任务中设置超时
                timeout = aiohttp.ClientTimeout(total=30)
                async with self.http_session.get(url, headers=headers, timeout=timeout,
                                                 allow_redirects=True) as response:
                    if response.status != 200:
                        logger.warning(f"备用方法获取失败: {response.status}, URL: {url}")
                        return None

                    content_type = response.headers.get('Content-Type', '')
                    logger.info(f"内容类型: {content_type}")

                    # 尝试获取文本内容，即使不是标准的HTML或JSON
                    try:
                        content = await response.text()
                        if content and len(content) > 500:  # 确保内容有足够长度
                            return content
                        return None
                    except Exception as text_error:
                        logger.warning(f"获取文本内容失败: {text_error}")
                        return None

            content = await asyncio.create_task(get_backup_content())

            if content:
                logger.info(f"备用方法获取内容成功: {url}, 内容长度: {len(content)}")
                return content
            return None
        except Exception as e:
            logger.error(f"备用方法获取URL内容失败: {e}")
            return None
    async def _upload_file_to_dify(self, file_path: str) -> Optional[str]:
        try:
            url = f"{self.dify_base_url}/files/upload"
            headers = {
                "Authorization": f"Bearer {self.dify_api_key}"
            }
            data = aiohttp.FormData()
            with open(file_path, "rb") as f:
                file_content = f.read()  # 读取文件内容到内存
            data.add_field("file", file_content, filename="video.mp4", content_type="video/mp4")
            data.add_field("user", "summary")
            async with self.http_session.post(
                url=url,
                headers=headers,
                data=data,
                proxy=self.http_proxy if self.http_proxy else None
            ) as response:
                if response.status == 200 or 201:
                    result = await response.json()
                    file_id = result.get("id")  # 假设返回的响应中包含文件 ID
                    logger.info(f"文件上传成功，文件ID: {file_id}")
                    if os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                        except Exception as e:
                            logger.warning(f"删除临时文件错误: {e}")
                    return file_id
                else:
                    if os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                        except Exception as e:
                            logger.warning(f"删除临时文件错误: {e}")
                    error_text = await response.text()
                    logger.error(f"文件上传失败: {response.status} - {error_text}")
                    return None
        except Exception as e:
            logger.error(f"文件上传时出错: {e}")
            return None
    async def _send_to_dify(self, content: str, is_xiaohongshu: bool = False,is_video = False) -> Optional[str]:
        if not self.dify_enable:
            return None
        headers = {
            "Authorization": f"Bearer {self.dify_api_key}",
            "Content-Type": "application/json"
        }
        url = f"{self.dify_base_url}/chat-messages"
        try:
            # 检查是否为GitHub个人主页
            is_github_profile = "github.com" in content and (
                        "overview" in content.lower() or "repositories" in content.lower())
            if is_video:
                video_id = await self._upload_file_to_dify(content)
                prompt = f"""请对以下视频进行详细全面的描述或者总结，提供丰富的信息：
                1. 📝 视频描述的内容，出现或者发生的事
                2. 🔑 详细的核心要点（5-7点，每点包含足够细节）
                3. 💡 如果有论述则阐述作者的主要观点、方法或建议（可选）
                4. 💰 实用价值和可行的行动建议（可选）
                5. 🏷️ 相关标签（3-5个）
                """
                payload = {
                         "inputs": {},
                    "files": [
                        {
                            "type": "video",
                            "transfer_method": "local_file",
                            "upload_file_id": video_id
                        }
                    ],
                    "query": prompt,
                    "response_mode": "blocking",
                    "conversation_id": None,
                    "user": "summary"
                }
            elif is_xiaohongshu:
                prompt = f"""请对以下小红书笔记进行详细全面的总结，提供丰富的信息：
    1. 📝 全面概括笔记的核心内容和主旨（2-3句话）
    2. 🔑 详细的核心要点（5-7点，每点包含足够细节）
    3. 💡 作者的主要观点、方法或建议（至少3点）
    4. 💰 实用价值和可行的行动建议
    5. 🏷️ 相关标签（3-5个）

    请确保总结内容详尽，捕捉原文中所有重要信息，不要遗漏关键点。

    原文内容：
    {content}
    """
                payload = {
                    "inputs": {},
                    "query": prompt,
                    "response_mode": "blocking",
                    "conversation_id": None,
                    "user": "summary"
                }
            elif 'arxiv' in content:
                prompt = f"""请对以下arxiv论文进行非常详细、全面的总结，确保涵盖所有重要信息：
                   1. 📝 论文的主要观点
                   2. 🔑 详细的关键要点核心内容
                   3. 💡 实验方法细节总结，实验数据处理
                   4. 📋 实验结果总结
                   5. 🎯 相比与传统的创新点
                   6. 🏷️ 相关领域标签（4-6个）
                   原文内容：
                   {content}
                   """
                payload = {
                    "inputs": {},
                    "query": prompt,
                    "response_mode": "blocking",
                    "conversation_id": None,
                    "user": "summary"
                }
            elif is_github_profile:
                prompt = f"""请对以下GitHub个人主页内容进行全面而详细的总结：
    1. 📝 开发者身份和专业领域的完整概述（3-4句话）
    2. 🔑 主要项目和贡献（列出所有可见的重要项目及其功能描述）
    3. 💻 技术栈和专业技能（尽可能详细列出所有提到的技术）
    4. 🚀 开发重点和特色项目（详细描述2-3个置顶项目）
    5. 📊 GitHub活跃度和贡献情况
    6. 🌟 个人成就和特色内容
    7. 🏷️ 技术领域标签（4-6个）

    请确保总结极其全面，不要遗漏任何重要细节，应包含个人简介、项目描述、技术栈等所有相关信息。

    原文内容：
    {content}
    """
                payload = {
                    "inputs": {},
                    "query": prompt,
                    "response_mode": "blocking",
                    "conversation_id": None,
                    "user": "summary"
                }
            else:
                prompt = f"""请对以下内容进行非常详细、全面的总结，确保涵盖所有重要信息：
    1. 📝 内容的完整主旨和核心内容（3-5句话）
    2. 🔑 详细的关键要点（5-8点，每点包含充分细节，不遗漏重要信息）
    3. 💡 主要观点、方法或价值（3-5点）
    4. 📋 模型的结构以及使用的数据集，或者实验得到了什么结果
    5. 🎯 相比与传统的创新点
    6. 🏷️ 相关领域标签（4-6个）
    
    请确保总结极其全面，每个要点都有足够的上下文和细节解释，不要简化或省略重要内容。
    总结应该是原始内容的完整缩影，让读者无需阅读原文也能获取所有关键信息。

    原文内容：
    {content}
    """
                payload = {
                    "inputs": {},
                    "query": prompt,
                    "response_mode": "blocking",
                    "conversation_id": None,
                    "user": "summary"
                }

            async with self.http_session.post(
                    url=url,
                    headers=headers,
                    json=payload,
                    proxy=self.http_proxy if self.http_proxy else None
            ) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get("answer", "")
                else:
                    error_text = await response.text()
                    logger.error(f"调用Dify API失败: {response.status} - {error_text}")
                    return None


        except Exception as e:
            logger.error(f"调用Dify API时出错: {e}")
            return None

    def _process_xml_message(self, event:AstrMessageEvent) -> Optional[Dict]:
        try:
            content = event.get_messages()
            msg_id = event.get_message_id()
            logger.info(f"插件处理XML消息: MsgId={msg_id}")
            # 检查内容是否为XML
            if not content.strip().startswith('<'):
                logger.warning("消息内容不是XML格式")
                return None
            logger.debug(f"完整XML内容: {content}")

            try:
                root = ET.fromstring(content)
                logger.info(f"解析XML根节点: {root.tag}")

                # 记录所有子节点以便调试
                for child in root:
                    logger.debug(f"子节点: {child.tag}")
            except ET.ParseError as e:
                logger.error(f"XML解析错误: {str(e)}")
                logger.error(f"XML内容片段: {content[:200]}...")
                return None

            appmsg = root.find('appmsg')
            if appmsg is None:
                logger.warning("未找到 appmsg 节点")
                return None

            logger.info("找到 appmsg 节点")

            # 记录appmsg的所有子节点
            for child in appmsg:
                logger.debug(f"appmsg子节点: {child.tag} = {child.text if child.text else ''}")

            title_elem = appmsg.find('title')
            des_elem = appmsg.find('des')
            url_elem = appmsg.find('url')
            type_elem = appmsg.find('type')

            title = title_elem.text if title_elem is not None and title_elem.text else ""
            description = des_elem.text if des_elem is not None and des_elem.text else ""
            url = url_elem.text if url_elem is not None and url_elem.text else None
            type_value = type_elem.text if type_elem is not None and type_elem.text else ""

            logger.info(f"提取的标题: {title}")
            logger.info(f"提取的描述: {description}")
            logger.info(f"提取的URL: {url}")
            logger.info(f"消息类型值: {type_value}")

            if url is None or not url.strip():
                logger.warning("URL为空，跳过处理")
                return None

            url = html.unescape(url)
            logger.info(f"处理后的URL: {url}")

            # 检查是否是小红书
            is_xiaohongshu = '<appname>小红书</appname>' in content
            if is_xiaohongshu:
                logger.info("检测到小红书卡片")

            result = {
                'title': title,
                'description': description,
                'url': url,
                'is_xiaohongshu': is_xiaohongshu,
                'type': type_value
            }
            logger.info(f"提取的信息: {result}")
            return result

        except ET.ParseError as e:
            logger.error(f"XML解析错误: {str(e)}")
            logger.error(f"XML内容片段: {content[:200] if 'content' in locals() else ''}...")
            return None
        except Exception as e:
            logger.error(f"处理XML消息时出错: {str(e)}")
            logger.exception(e)
            return None

    async def _process_url(self, url: str) -> Optional[str]:
        try:
            if url.endswith(".mp4"):
                return await self._send_to_dify(url,is_video=True)
            videos = ["bilibili", "youtube"]
            url_content = ""
            for video in videos:
                if video in url:
                    url_content = await self.get_videos(url) #返回缓存的视频路径
                    return await self._send_to_dify(url_content)
            if 'arxiv' in url:
                url_content += "\narxiv论文具体内容如下:\n"
                url_content += await self.get_arxiv_paper_text(url)
            elif 'github' in url:
                url_content += "\ngithub项目代码内容如下:\n"
                url_content += await self.get_github_code_text(url)
            else:
                url_content += await self._fetch_url_content(url)
            if not url_content:
                return None
            url_content = html.unescape(url_content)
            return await self._send_to_dify(url_content)
        except Exception as e:
            logger.error(f"处理URL时出错: {e}")
            return None

    async def _handle_card_message(self,event: AstrMessageEvent, info: Dict) -> bool:
        chat_id = event.get_sender_name()
        try:
            # 获取URL内容
            url = info['url']
            logger.info(f"开始获取卡片URL内容: {url}")
            url_content = await self._fetch_url_content(url)

            if not url_content:
                logger.warning(f"无法获取卡片内容: {url}")
                return False

            logger.info(f"成功获取卡片内容，长度: {len(url_content)}")

            # 构建要总结的内容
            content_to_summarize = f"""
      标题：{info['title']}
      描述：{info['description']}
      正文：{url_content}
      """
            # 调用Dify API生成总结
            is_xiaohongshu = info.get('is_xiaohongshu', False)
            logger.info(f"开始生成总结, 是否小红书: {is_xiaohongshu}")
            summary = await self._send_to_dify(content_to_summarize, is_xiaohongshu=is_xiaohongshu)

            if not summary:
                logger.error("生成总结失败")
                return False

            logger.info(f"成功生成总结，长度: {len(summary)}")

            # 根据卡片类型设置前缀
            prefix = "🎯 小红书笔记详细总结如下" if is_xiaohongshu else "🎯 卡片内容详细总结如下"

            logger.info("总结已发送")
            return False  # 阻止后续处理

        except Exception as e:
            logger.error(f"处理卡片消息时出错: {e}")
            logger.exception(e)  # 记录完整堆栈信息
            return False

    async def handle_article_message(self, event,message: Dict) -> bool:
        """处理文章类型消息（微信公众号文章等）"""
        if not self.dify_enable:
            return
        content = event.get_messages()
        chat_id = event.get_message_id()
        try:
            card_info = self._process_xml_message(message)
            if not card_info:
                logger.warning("文章消息解析失败")
                return

            logger.info(f"识别为文章消息: {card_info['title']}")

            # 存储卡片信息供后续使用
            self.recent_cards[chat_id] = {
                "info": card_info,
                "timestamp": time.time()
            }
            logger.info(f"已存储文章信息: {card_info['title']} 供后续总结使用")
            event.plain_result("📰 检测到文章，发送\"/总结\"命令可以生成内容总结")

            return
        except Exception as e:
            logger.error(f"处理文章消息时出错: {e}")
            logger.exception(e)
            return

    async def handle_file_message(self, event, message: Dict) -> bool:
        """处理文件类型消息（包括卡片消息）"""
        if not self.dify_enable:
            return

        chat_id = message.get("FromWxid", "")
        msg_type = message.get("MsgType", 0)

        # 检查是否是卡片消息（类型49）
        if msg_type != 49:
            logger.info(f"非卡片消息，跳过处理: MsgType={msg_type}")
            return

        logger.info(f"收到卡片消息: MsgType={msg_type}, chat_id={chat_id}")

        try:
            # 处理XML消息
            card_info = self._process_xml_message(message)
            if not card_info:
                logger.warning("卡片消息解析失败")
                return

            logger.info(f"识别为卡片消息: {card_info['title']}")

            # 存储卡片信息供后续使用
            self.recent_cards[chat_id] = {
                "info": card_info,
                "timestamp": time.time()
            }
            logger.info(f"已存储卡片信息: {card_info['title']} 供后续总结使用")
            event.plain_result(chat_id, "📎 检测到卡片，发送\"/总结\"命令可以生成内容总结")
        except Exception as e:
            logger.error(f"处理文件消息时出错: {e}")
            logger.exception(e)


    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""


    @filter.command("summarize")
    async def summarize (self, event: AstrMessageEvent):
        """这是一个 summarize指令"""
        logger.info("使用总结")
        message_chain = event.get_messages()
        summarize = 1
        for msg in message_chain:
            if msg.type == 'Reply':
                yield event.plain_result("视频总结调试中，请勿占用回复")
                # 处理回复消息
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                assert isinstance(event, AiocqhttpMessageEvent)
                client = event.bot
                payload = {
                    "message_id": msg.id
                }
                response = await client.api.call_action('get_msg', **payload)  # 调用 协议端  API
                reply_msg = response['message']

                for msg in reply_msg:
                    if msg['type'] == 'video':
                        summarize = 0
                        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                        assert isinstance(event, AiocqhttpMessageEvent)
                        client = event.bot
                        payloads2 = {
                            "file_id": msg['data']['file']
                        }
                        response = await client.api.call_action('get_file', **payloads2)
                        localdiskpath = response['file']
                        summary = await self._process_url(localdiskpath)
                        if summary:
                            node = Node(
                                uin="3967575984",
                                name="whyis实在",
                                content=[
                                    Plain(f"🎯 详细内容总结如下：\n\n{summary}"),
                                    Image.fromFileSystem("./data/plugins/summary-master/mizunashi.jpg")
                                ]
                            )
                            yield event.chain_result([node])
                        yield event.plain_result("您指定的视频已经收到了喵~")
        if summarize:
            content = message_chain[0]
            text = content.text[10:]
            chat_id = event.get_sender_name()

            urls = re.findall(self.URL_PATTERN, text)
            if urls:
                url = urls[0]
                yield event.plain_result(f"找到URL，正在为您生成详细内容总结")
                try:
                    summary = await self._process_url(url)
                    if summary:
                        node = Node(
                            uin="3967575984",
                            name="whyis实在",
                            content=[
                                Plain(f"🎯 详细内容总结如下：\n\n{summary}"),
                                Image.fromFileSystem("./data/plugins/summary-master/mizunashi.jpg")
                            ]
                        )
                        yield event.chain_result([node])
                        # 总结后删除该URL
                        del self.recent_urls[chat_id]
                    else:
                        yield event.plain_result("❌ 抱歉，生成总结失败")
                except Exception as e:
                    logger.error(f"处理URL时出错: {e}")
                    event.plain_result("❌ 抱歉，处理过程中出现错误")
            else:
                yield event.plain_result(f"没有找到URL")

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
