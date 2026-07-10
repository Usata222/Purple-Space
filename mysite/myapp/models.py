from django.db import models
from django.contrib.auth.models import User


class ChatRoom(models.Model):
    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_rooms')
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    members = models.ManyToManyField(User, related_name='rooms', blank=True)

    def __str__(self):
        return self.name


class ChatMessage(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name='messages')
    message_content = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    reply_to = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='replies')

    is_edited = models.BooleanField(default=False)
    is_deleted = models.BooleanField(default=False)

    file = models.FileField(upload_to='chat_files/%Y/%m/', null=True, blank=True)
    file_name = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ('timestamp',)

    def __str__(self):
        return f'{self.user.username}: {self.message_content[:30]}'


class MessageReaction(models.Model):
    message = models.ForeignKey(ChatMessage, on_delete=models.CASCADE, related_name='reactions')
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    emoji = models.CharField(max_length=8)

    class Meta:
        unique_together = ('message', 'user', 'emoji')


class RoomInvite(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('declined', 'Declined'),
    ]
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name='invites')
    invited_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_invites')
    invited_user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='received_invites')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ('-created_at',)

    def __str__(self):
        return f'{self.invited_user.username} -> {self.room.name} ({self.status})'


class Notification(models.Model):
    TYPE_CHOICES = [
        ('room_invite', 'Room invite'),
    ]
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    notif_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default='room_invite')
    message = models.CharField(max_length=255)
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, null=True, blank=True)
    invite = models.ForeignKey(RoomInvite, on_delete=models.CASCADE, null=True, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('created_at',)  # oldest first

    def __str__(self):
        return f'{self.recipient.username}: {self.message[:40]}'
