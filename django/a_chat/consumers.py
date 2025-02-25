import json
from logging import getLogger

import jwt
from a_core.db import execute_query, init_db
from channels.exceptions import StopConsumer
from channels.generic.websocket import AsyncWebsocketConsumer

from django.conf import settings

logger = getLogger(__name__)


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        try:
            # URL 쿼리 파라미터에서 토큰 추출
            query_string = self.scope.get("query_string", b"").decode()
            query_params = dict(
                param.split("=") for param in query_string.split("&") if param
            )
            token = query_params.get("token")

            if not token:
                logger.error("토큰이 없습니다.")
                await self.close()
                return

            # JWT 토큰 디코드
            try:
                payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
                user_id = payload.get("user_id")

                # 유저 정보 저장
                self.user_id = user_id

                # 여기서부터 DB 연결 로직
                await init_db()
                self.room_id = self.scope["url_route"]["kwargs"]["room_id"]
                self.room_group_name = f"chat_{self.room_id}"

                # 유저정보 가져오기
                user_record = await execute_query(
                    """
                    SELECT * FROM users WHERE id = $1
                """,
                    int(self.user_id),
                )

                self.user_info = {}
                self.user_info["id"] = user_record[0]["id"]
                self.user_info["username"] = user_record[0]["username"]
                self.user_info["email"] = user_record[0]["email"]

                # 채팅방이 있는지 검사해서, 없으면 연결끊기
                chat_room = await execute_query(
                    """
                    SELECT * FROM chat_room WHERE id = $1
                """,
                    int(self.room_id),
                )

                item = await execute_query(
                    """
                    SELECT * FROM product_detail WHERE id = $1
                """,
                    int(chat_room[0]["item_id"]),
                )

                prev_messages = await execute_query(
                    """
                    SELECT cm.*, u.username as sender_username, u.email as sender_email 
                    FROM chat_message cm
                    JOIN users u ON cm.sender_id = u.id
                    WHERE cm.chat_room_id = $1
                    """,
                    int(self.room_id),
                )

                # Record 객체를 딕셔너리로 변환
                formatted_messages = []
                for msg in prev_messages:
                    formatted_messages.append(
                        {
                            "id": msg["id"],
                            "message": msg["message"],
                            "sender": {
                                "id": msg["sender_id"],
                                "username": msg["sender_username"],
                                "email": msg["sender_email"],
                            },
                            "chat_room_id": msg["chat_room_id"],
                            "created_at": msg["created_at"].isoformat(),
                            "updated_at": msg["updated_at"].isoformat(),
                        }
                    )

                buyer_id = chat_room[0]["buyer_id"]
                seller_id = item[0]["user"]

                if (not buyer_id == self.user_id) and (not seller_id == self.user_id):
                    logger.error("접근 권한이 없습니다.")
                    await self.close()
                    return

                # chat_room이 리스트로 반환되므로, 존재 여부만 확인
                if not chat_room:
                    logger.error("채팅방이 없습니다.")
                    await self.close()
                    return

                # 채널 레이어 연결 상태 확인
                if not self.channel_layer:
                    logger.error("Channel layer is not available")
                    await self.close()
                    return

                # 채널 레이어에 그룹 추가
                try:
                    await self.channel_layer.group_add(
                        self.room_group_name, self.channel_name
                    )
                except Exception as e:
                    logger.error(f"Group add error: {str(e)}")
                    await self.close()
                    return

                await self.accept()
                await self.send(
                    text_data=json.dumps(
                        {
                            "type": "connection_established",
                            "message": "Connected to chat room",
                            "user_info": self.user_info,
                            "prev_messages": formatted_messages,  # 변환된 메시지 사용
                        }
                    )
                )

            except jwt.ExpiredSignatureError:
                logger.error("Token has expired. 토큰이 만료되었습니다.")
                await self.send(
                    text_data=json.dumps(
                        {
                            "type": "error",
                            "message": "토큰이 만료되었습니다. 다시 로그인해주세요.",
                        }
                    )
                )
                await self.close()
                return
            except jwt.InvalidTokenError:
                logger.error("Invalid token. 유효하지 않은 토큰입니다.")
                await self.send(
                    text_data=json.dumps(
                        {"type": "error", "message": "유효하지 않은 토큰입니다."}
                    )
                )
                await self.close()
                return

        except Exception as e:
            logger.error(f"Connection error: {str(e)}")
            await self.close()

    async def disconnect(self, close_code):
        try:
            # 그룹에서 채널 제거
            await self.channel_layer.group_discard(
                self.room_group_name, self.channel_name
            )
        except Exception as e:
            logger.error(f"Disconnection error: {str(e)}")
        finally:
            raise StopConsumer()

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            message = data.get("message")
            sender = self.user_id

            if not message or not sender:
                raise ValueError("Message and sender are required")

            # SQL 인젝션 방지를 위해 execute_query 사용
            await execute_query(
                """
                INSERT INTO chat_message (sender_id, chat_room_id, message, created_at, updated_at)
                VALUES ($1, $2, $3, NOW(), NOW())
            """,
                int(sender),
                int(self.room_id),
                message,
            )

            print(self.user_info)

            if not self.user_info:
                raise ValueError("User information not found")

            await self.channel_layer.group_send(
                self.room_group_name,
                {"type": "chat_message", "message": message, "sender": self.user_info},
            )
        except json.JSONDecodeError:
            logger.error("Invalid JSON format")
            await self.send(
                text_data=json.dumps(
                    {"type": "error", "message": "Invalid message format"}
                )
            )
        except ValueError as e:
            logger.error(f"Validation error: {e}")
            await self.send(text_data=json.dumps({"type": "error", "message": str(e)}))
        except Exception as e:
            logger.error(f"Message processing error: {e}")
            await self.send(
                text_data=json.dumps(
                    {"type": "error", "message": "Failed to process message"}
                )
            )

    async def chat_message(self, event):
        try:
            await self.send(
                text_data=json.dumps(
                    {"message": event["message"], "sender": event["sender"]}
                )
            )
        except Exception as e:
            print(f"Message send error: {str(e)}")
