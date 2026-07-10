from channels.generic.websocket import AsyncWebsocketConsumer
import json
from collections import defaultdict
from asgiref.sync import sync_to_async
from .models import ChatMessage, ChatRoom, MessageReaction
from django.contrib.auth.models import User

# NOTE: in-memory presence tracking. Fine for a single dev server process
# (InMemoryChannelLayer already assumes this); would need Redis-backed
# storage for a real multi-process deployment.
ROOM_PRESENCE = defaultdict(lambda: defaultdict(set))  # room_slug -> username -> {channel_names}


def online_usernames(room_slug):
    return sorted(ROOM_PRESENCE[room_slug].keys())


def serialize_reactions(message):
    reactions = defaultdict(list)
    for r in message.reactions.all():
        reactions[r.emoji].append(r.user.username)
    return reactions


class ChatConsumer(AsyncWebsocketConsumer):
    GLOBAL_NOTIFY_GROUP = 'global_notify'

    async def connect(self):
        self.user = self.scope['user']

        if not self.user.is_authenticated:
            await self.close()
            return

        self.room_name = self.scope['url_route']['kwargs']['room_name']
        self.room_group_name = 'chat_%s' % self.room_name

        is_member = await self.user_is_room_member(self.room_name)
        if not is_member:
            await self.close()
            return

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.channel_layer.group_add(self.GLOBAL_NOTIFY_GROUP, self.channel_name)
        await self.accept()

        ROOM_PRESENCE[self.room_name][self.user.username].add(self.channel_name)
        await self.broadcast_presence()

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
            await self.channel_layer.group_discard(self.GLOBAL_NOTIFY_GROUP, self.channel_name)

            user_channels = ROOM_PRESENCE[self.room_name].get(self.user.username)
            if user_channels is not None:
                user_channels.discard(self.channel_name)
                if not user_channels:
                    del ROOM_PRESENCE[self.room_name][self.user.username]
            await self.broadcast_presence()

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        msg_type = data.get('type', 'message')

        if msg_type == 'message':
            await self.handle_message(data)
        elif msg_type == 'typing':
            await self.handle_typing(data)
        elif msg_type == 'edit':
            await self.handle_edit(data)
        elif msg_type == 'delete':
            await self.handle_delete(data)
        elif msg_type == 'react':
            await self.handle_react(data)

    # ---------- message send ----------
    async def handle_message(self, data):
        message = (data.get('message') or '').strip()
        if not message:
            return
        reply_to_id = data.get('reply_to')

        saved = await self.save_message(message, reply_to_id)
        if saved is None:
            return

        await self.channel_layer.group_send(self.room_group_name, {
            'type': 'chat_message',
            **saved,
        })

        await self.channel_layer.group_send(self.GLOBAL_NOTIFY_GROUP, {
            'type': 'notify_message',
            'room_slug': self.room_name,
            'room_name': saved.get('room_name', self.room_name),
            'username': self.user.username,
            'message': message,
            'exclude_channel': self.channel_name,
        })

    @sync_to_async
    def save_message(self, message, reply_to_id):
        try:
            room = ChatRoom.objects.get(slug=self.room_name)
        except ChatRoom.DoesNotExist:
            return None

        reply_to = None
        if reply_to_id:
            reply_to = ChatMessage.objects.filter(id=reply_to_id, room=room).first()

        msg = ChatMessage.objects.create(
            user=self.user, room=room, message_content=message, reply_to=reply_to
        )

        reply_payload = None
        if reply_to:
            reply_payload = {
                'id': reply_to.id,
                'username': reply_to.user.username,
                'snippet': (reply_to.message_content or '[file]')[:80],
            }

        return {
            'id': msg.id,
            'username': self.user.username,
            'message': msg.message_content,
            'timestamp': msg.timestamp.strftime('%H:%M'),
            'reply_to': reply_payload,
            'file_url': None,
            'file_name': None,
            'room_name': room.name,
        }

    # ---------- typing ----------
    async def handle_typing(self, data):
        await self.channel_layer.group_send(self.room_group_name, {
            'type': 'typing_status',
            'username': self.user.username,
            'is_typing': bool(data.get('is_typing')),
            'sender_channel': self.channel_name,
        })

    # ---------- edit ----------
    async def handle_edit(self, data):
        message_id = data.get('message_id')
        new_content = (data.get('message') or '').strip()
        if not message_id or not new_content:
            return
        ok = await self.edit_message(message_id, new_content)
        if ok:
            await self.channel_layer.group_send(self.room_group_name, {
                'type': 'message_edited',
                'id': message_id,
                'message': new_content,
            })

    @sync_to_async
    def edit_message(self, message_id, new_content):
        try:
            msg = ChatMessage.objects.get(id=message_id, room__slug=self.room_name)
        except ChatMessage.DoesNotExist:
            return False
        if msg.user_id != self.user.id or msg.is_deleted:
            return False
        msg.message_content = new_content
        msg.is_edited = True
        msg.save(update_fields=['message_content', 'is_edited'])
        return True

    # ---------- delete ----------
    async def handle_delete(self, data):
        message_id = data.get('message_id')
        if not message_id:
            return
        ok = await self.delete_message(message_id)
        if ok:
            await self.channel_layer.group_send(self.room_group_name, {
                'type': 'message_deleted',
                'id': message_id,
            })

    @sync_to_async
    def delete_message(self, message_id):
        try:
            msg = ChatMessage.objects.get(id=message_id, room__slug=self.room_name)
        except ChatMessage.DoesNotExist:
            return False
        if msg.user_id != self.user.id:
            return False
        msg.is_deleted = True
        msg.message_content = ''
        msg.save(update_fields=['message_content', 'is_deleted'])
        return True

    # ---------- reactions ----------
    async def handle_react(self, data):
        message_id = data.get('message_id')
        emoji = data.get('emoji')
        if not message_id or not emoji:
            return
        reactions = await self.toggle_reaction(message_id, emoji)
        if reactions is None:
            return
        await self.channel_layer.group_send(self.room_group_name, {
            'type': 'reaction_update',
            'message_id': message_id,
            'reactions': reactions,
        })

    @sync_to_async
    def toggle_reaction(self, message_id, emoji):
        try:
            msg = ChatMessage.objects.get(id=message_id, room__slug=self.room_name)
        except ChatMessage.DoesNotExist:
            return None
        existing = MessageReaction.objects.filter(message=msg, user=self.user, emoji=emoji).first()
        if existing:
            existing.delete()
        else:
            MessageReaction.objects.create(message=msg, user=self.user, emoji=emoji)
        return dict(serialize_reactions(msg))

    async def broadcast_presence(self):
        await self.channel_layer.group_send(self.room_group_name, {
            'type': 'presence_update',
            'online_users': online_usernames(self.room_name),
        })

    # ---------- outgoing event handlers (channel layer -> client) ----------
    async def chat_message(self, event):
        payload = {k: v for k, v in event.items() if k != 'type' and k != 'room_name'}
        payload['type'] = 'chat_message'
        await self.send(text_data=json.dumps(payload))

    async def typing_status(self, event):
        if event.get('sender_channel') == self.channel_name:
            return  # don't echo typing status back to the person typing
        await self.send(text_data=json.dumps({
            'type': 'typing_status',
            'username': event['username'],
            'is_typing': event['is_typing'],
        }))

    async def presence_update(self, event):
        await self.send(text_data=json.dumps({
            'type': 'presence_update',
            'online_users': event['online_users'],
        }))

    async def message_edited(self, event):
        await self.send(text_data=json.dumps({
            'type': 'message_edited',
            'id': event['id'],
            'message': event['message'],
        }))

    async def message_deleted(self, event):
        await self.send(text_data=json.dumps({
            'type': 'message_deleted',
            'id': event['id'],
        }))

    async def reaction_update(self, event):
        await self.send(text_data=json.dumps({
            'type': 'reaction_update',
            'message_id': event['message_id'],
            'reactions': event['reactions'],
        }))

    async def notify_message(self, event):
        if event.get('exclude_channel') == self.channel_name:
            return
        if event['room_slug'] == getattr(self, 'room_name', None):
            return  # already seeing it live in this room, no toast needed
        await self.send(text_data=json.dumps({
            'type': 'notify_message',
            'room_slug': event['room_slug'],
            'room_name': event['room_name'],
            'username': event['username'],
            'message': event['message'],
        }))

    @sync_to_async
    def user_is_room_member(self, slug):
        return ChatRoom.objects.filter(slug=slug, members=self.user).exists()
