# -*- coding: utf-8 -*-

"""
Xueqiu RPA Backend
Handles high-level automation flow for posting to xueqiu.com using CDP.
"""

import json
import logging
import asyncio
from typing import Any

from .cdp_client import CdpConnection, get_or_create_tab
from .. import config as trader_config

logger = logging.getLogger(__name__)

class XueqiuRpaBackend:
    """雪球网页端 RPA 自动化控制器"""

    def __init__(self):
        cfg = trader_config.load()
        self.port = cfg.chrome_cdp_port or 9222

    async def publish_review(self, content: str, semi_manual: bool = True) -> dict[str, Any]:
        """通过 CDP 在雪球网页发布复盘/调仓状态
        
        Args:
            content: 待发布文案
            semi_manual: 是否开启半人工确认模式（默认为 True）
        """
        logger.info("[Xueqiu RPA] 开始发帖任务，模式: %s", "半人工确认" if semi_manual else "全自动发布")
        
        ws_url = None
        conn = None
        try:
            # 1. 获取或新建雪球活动 Tab (端口不在线会自动唤醒)
            ws_url = await get_or_create_tab("xueqiu.com", "https://xueqiu.com", self.port)
            
            # 2. 建立 CDP WebSocket 连接
            conn = CdpConnection(ws_url)
            await conn.connect()
            
            # 3. 检查登录状态：寻找发帖输入框是否存在
            # 支持通用 Textarea 与 React / 传统输入 DOM 结构
            detect_script = """
            (() => {
                let el = document.querySelector('textarea') || 
                         document.querySelector('[placeholder*="今天想说点什么"]') || 
                         document.querySelector('[placeholder*="分享我的"]') ||
                         document.querySelector('div[contenteditable="true"]');
                return el ? true : false;
            })()
            """
            is_logged_in = await conn.execute_js(detect_script)
            
            if not is_logged_in:
                logger.warning("[Xueqiu RPA] 未在网页中侦测到发帖框，判定为未登录状态")
                return {
                    "code": 1,
                    "error": "未检测到雪球登录态！已为您在浏览器中拉起雪球首页，请先完成登录后再试该指令。"
                }

            # 4. 注入文本内容 (使用 React 属性设置 Hack，兼容网页框架的 State 状态绑定)
            content_escaped = json.dumps(content, ensure_ascii=False)
            fill_script = f"""
            (() => {{
                let el = document.querySelector('textarea') || 
                         document.querySelector('[placeholder*="今天想说点什么"]') || 
                         document.querySelector('[placeholder*="分享我的"]') ||
                         document.querySelector('div[contenteditable="true"]');
                if (!el) return false;
                
                if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {{
                    // React value setter hack: 触发原生组件状态绑定
                    let nativeValueSetter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set
                                         || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
                    if (nativeValueSetter) {{
                        nativeValueSetter.call(el, {content_escaped});
                    }} else {{
                        el.value = {content_escaped};
                    }}
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }} else {{
                    el.innerText = {content_escaped};
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                }}
                el.focus();
                return true;
            }})()
            """
            fill_ok = await conn.execute_js(fill_script)
            if not fill_ok:
                raise RuntimeError("往网页发帖输入框填入文案失败")

            logger.info("[Xueqiu RPA] 已成功向发帖框注入文案")

            # 5. 执行发帖动作决策
            if semi_manual:
                # 半人工确认模式：闪烁高亮“发布”按钮，并让输入框获取焦点，等待用户在屏幕前点击
                highlight_script = """
                (() => {
                    let btns = Array.from(document.querySelectorAll('button, a, .Home_post_btn, [class*="post_btn"], [class*="publish"]'));
                    let publish_btn = btns.find(b => b.textContent.includes('发布') || b.className.includes('post_btn') || b.className.includes('publish'));
                    if (publish_btn) {
                        // 红色虚线脉冲呼吸灯效果
                        publish_btn.style.border = '4px dashed #ff4d4f';
                        publish_btn.style.outline = 'none';
                        publish_btn.style.animation = 'guling_pulse 1.2s infinite';
                        
                        if (!document.getElementById('guling_rpa_style')) {
                            let style = document.createElement('style');
                            style.id = 'guling_rpa_style';
                            style.innerHTML = '@keyframes guling_pulse { 0% { transform: scale(1); box-shadow: 0 0 0 0 rgba(255, 77, 79, 0.7); } 70% { transform: scale(1.05); box-shadow: 0 0 0 10px rgba(255, 77, 79, 0); } 100% { transform: scale(1); box-shadow: 0 0 0 0 rgba(255, 77, 79, 0); } }';
                            document.head.appendChild(style);
                        }
                        
                        // 页面平滑滚动聚焦到发帖区域
                        publish_btn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        return true;
                    }
                    return false;
                })()
                """
                await conn.execute_js(highlight_script)
                logger.info("[Xueqiu RPA] 半人工模式：高亮显示网页发布按钮，等待用户手动点击")
                return {
                    "code": 0,
                    "status": "pending_manual_click",
                    "msg": "文案已成功自动填入雪球网页发帖框，并在页面中以「红色脉冲呼吸灯」高亮了发布按钮。请在浏览器中核对并手动点击“发布”。"
                }
            else:
                # 全自动发布模式：模拟物理级点击
                publish_script = """
                (() => {
                    let btns = Array.from(document.querySelectorAll('button, a, .Home_post_btn, [class*="post_btn"], [class*="publish"]'));
                    let publish_btn = btns.find(b => b.textContent.includes('发布') || b.className.includes('post_btn') || b.className.includes('publish'));
                    if (publish_btn) {
                        publish_btn.click();
                        return true;
                    }
                    return false;
                })()
                """
                # 给页面一点点缓冲，确保 React 表单状态渲染生效后再点击
                await asyncio.sleep(0.3)
                publish_ok = await conn.execute_js(publish_script)
                if not publish_ok:
                    raise RuntimeError("未在页面中找到‘发布’按钮，无法执行自动发帖")
                
                logger.info("[Xueqiu RPA] 全自动模式：发帖按钮已自动模拟点击完成")
                return {
                    "code": 0,
                    "status": "success",
                    "msg": "调仓日志/复盘文案已成功通过浏览器 CDP 自动发布到您的雪球账户！"
                }

        except Exception as e:
            logger.error("[Xueqiu RPA] 发帖任务执行异常: %s", e)
            return {
                "code": 1,
                "error": f"雪球 RPA 执行失败: {e}"
            }
        finally:
            if conn:
                await conn.close()
