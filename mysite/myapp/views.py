from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import login, views as auth_views
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils.text import slugify
from django.utils import timezone
from django.contrib import messages
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from datetime import timedelta
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.conf import settings

from .models import ChatRoom, ChatMessage, RoomInvite, Notification


def default_landing_url(user):
    """Where a user should land right after logging in: the configured
    default room if it exists and they're a member of it, otherwise the
    room list (home)."""
    slug = getattr(settings, 'DEFAULT_ROOM_SLUG', None)
    if slug:
        room = ChatRoom.objects.filter(slug=slug, members=user).first()
        if room:
            return reverse('chatroom', args=[room.slug])
    return reverse('index')


class PurpleLoginView(auth_views.LoginView):
    template_name = 'myapp/login.html'

    def get_success_url(self):
        return default_landing_url(self.request.user)


def signup(request):
    if request.user.is_authenticated:
        return redirect(default_landing_url(request.user))
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            # New users automatically get the default room (if configured)
            # so they land somewhere with content instead of an empty homepage.
            default_slug = getattr(settings, 'DEFAULT_ROOM_SLUG', None)
            if default_slug:
                default_room = ChatRoom.objects.filter(slug=default_slug).first()
                if default_room:
                    default_room.members.add(user)
            return redirect(default_landing_url(user))
    else:
        form = UserCreationForm()
    return render(request, 'myapp/signup.html', {'form': form})


def post_login(request):
    """Named target for LOGIN_REDIRECT_URL; just forwards to the same
    default-room-or-home logic used everywhere else."""
    if not request.user.is_authenticated:
        return redirect('login')
    return redirect(default_landing_url(request.user))


@login_required
def index(request):
    chatrooms = ChatRoom.objects.filter(members=request.user).distinct()

    one_hour_ago = timezone.now() - timedelta(hours=1)
    banner_notifications = (
        Notification.objects.filter(recipient=request.user, is_read=False, created_at__gte=one_hour_ago)
        .select_related('room', 'invite', 'invite__invited_by')
    )
    unread_count = Notification.objects.filter(recipient=request.user, is_read=False).count()

    return render(request, 'myapp/modern-index.html', {
        'chatrooms': chatrooms,
        'banner_notifications': banner_notifications,
        'unread_count': unread_count,
    })


@login_required
def chatroom(request, slug):
    chatroom = get_object_or_404(ChatRoom, slug=slug)
    if not chatroom.members.filter(id=request.user.id).exists():
        messages.error(request, "You don't have access to that room yet.")
        return redirect('index')

    messages_qs = (
        ChatMessage.objects.filter(room=chatroom)
        .select_related('user', 'reply_to', 'reply_to__user')
        .prefetch_related('reactions', 'reactions__user')[:100]
    )

    serialized = []
    for m in messages_qs:
        reactions = {}
        for r in m.reactions.all():
            reactions.setdefault(r.emoji, []).append(r.user.username)
        reply_to = None
        if m.reply_to_id and m.reply_to:
            reply_to = {
                'id': m.reply_to.id,
                'username': m.reply_to.user.username,
                'snippet': (m.reply_to.message_content or '[file]')[:80],
            }
        serialized.append({
            'id': m.id,
            'username': m.user.username,
            'message': '' if m.is_deleted else m.message_content,
            'timestamp': m.timestamp.strftime('%H:%M'),
            'reply_to': reply_to,
            'file_url': m.file.url if m.file else None,
            'file_name': m.file_name or None,
            'is_edited': m.is_edited,
            'is_deleted': m.is_deleted,
            'reactions': reactions,
        })

    return render(request, 'myapp/modern-room.html', {
        'chatroom': chatroom,
        'messages_json': serialized,
    })


@login_required
def create_room(request):
    other_users = User.objects.exclude(id=request.user.id).order_by('username')

    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        invitee_ids = request.POST.getlist('invitees')

        if not name:
            messages.error(request, 'Room name cannot be empty.')
            return redirect('create_room')
        slug = slugify(name)
        if ChatRoom.objects.filter(slug=slug).exists():
            messages.error(request, 'A room with that name already exists.')
            return redirect('create_room')

        room = ChatRoom.objects.create(name=name, slug=slug, created_by=request.user)
        room.members.add(request.user)

        for uid in invitee_ids:
            invited_user = User.objects.filter(id=uid).exclude(id=request.user.id).first()
            if not invited_user:
                continue
            invite = RoomInvite.objects.create(room=room, invited_by=request.user, invited_user=invited_user)
            Notification.objects.create(
                recipient=invited_user,
                notif_type='room_invite',
                message=f'{request.user.username} invited you to join "{room.name}"',
                room=room,
                invite=invite,
            )

        return redirect('chatroom', slug=room.slug)

    return render(request, 'myapp/create_room.html', {'other_users': other_users})


@login_required
def notifications_view(request):
    notifs = (
        Notification.objects.filter(recipient=request.user)
        .select_related('room', 'invite', 'invite__invited_by')
    )
    Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
    return render(request, 'myapp/notifications.html', {'notifications': notifs})


@login_required
@require_POST
def accept_invite(request, invite_id):
    invite = get_object_or_404(RoomInvite, id=invite_id, invited_user=request.user)
    if invite.status == 'pending':
        invite.status = 'accepted'
        invite.responded_at = timezone.now()
        invite.save(update_fields=['status', 'responded_at'])
        invite.room.members.add(request.user)
        Notification.objects.filter(invite=invite).update(is_read=True)
    return redirect('chatroom', slug=invite.room.slug)


@login_required
@require_POST
def decline_invite(request, invite_id):
    invite = get_object_or_404(RoomInvite, id=invite_id, invited_user=request.user)
    if invite.status == 'pending':
        invite.status = 'declined'
        invite.responded_at = timezone.now()
        invite.save(update_fields=['status', 'responded_at'])
        Notification.objects.filter(invite=invite).update(is_read=True)
    next_url = request.POST.get('next') or reverse('index')
    return redirect(next_url)


MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB


@login_required
@require_POST
def upload_file(request, slug):
    room = get_object_or_404(ChatRoom, slug=slug)
    if not room.members.filter(id=request.user.id).exists():
        return JsonResponse({'error': 'Not a member of this room.'}, status=403)

    uploaded = request.FILES.get('file')
    if not uploaded:
        return JsonResponse({'error': 'No file provided.'}, status=400)
    if uploaded.size > MAX_UPLOAD_SIZE:
        return JsonResponse({'error': 'File too large (10MB max).'}, status=400)

    msg = ChatMessage.objects.create(
        user=request.user,
        room=room,
        message_content='',
        file=uploaded,
        file_name=uploaded.name,
    )

    payload = {
        'type': 'chat_message',
        'id': msg.id,
        'username': request.user.username,
        'message': '',
        'timestamp': msg.timestamp.strftime('%H:%M'),
        'reply_to': None,
        'file_url': msg.file.url,
        'file_name': msg.file_name,
    }

    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)('chat_%s' % slug, payload)
    async_to_sync(channel_layer.group_send)('global_notify', {
        'type': 'notify_message',
        'room_slug': slug,
        'room_name': room.name,
        'username': request.user.username,
        'message': f'📎 {uploaded.name}',
        'exclude_channel': None,
    })

    return JsonResponse({'ok': True, 'id': msg.id, 'file_url': msg.file.url, 'file_name': msg.file_name})
