from decimal import Decimal
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.contrib import messages
from django.db import transaction
from django.db.models import Sum, Count, Q, F
from django.db.models.functions import Coalesce
from django.utils import timezone
from .models import *
from .forms import *


def event_list(request):
    events = Event.objects.filter(is_active=True, date__gte=timezone.now())\
                          .select_related('category', 'venue')

    q = request.GET.get('q', '').strip()
    category_id = request.GET.get('category', '')
    date_from = request.GET.get('date_from', '')
    date_to = request.GET.get('date_to', '')
    price_min = request.GET.get('price_min', '')
    price_max = request.GET.get('price_max', '')
    sort = request.GET.get('sort', '')

    if q:
        events = events.filter(Q(title__icontains=q) | Q(description__icontains=q))

    if category_id:
        events = events.filter(category_id=category_id)

    if date_from:
        try:
            events = events.filter(date__date__gte=date_from)
        except ValueError:
            pass

    if date_to:
        try:
            events = events.filter(date__date__lte=date_to)
        except ValueError:
            pass

    if price_min:
        try:
            events = events.filter(price__gte=float(price_min))
        except ValueError:
            pass

    if price_max:
        try:
            events = events.filter(price__lte=float(price_max))
        except ValueError:
            pass

    if sort == 'price_asc':
        events = events.order_by('price')
    elif sort == 'price_desc':
        events = events.order_by('-price')
    elif sort == 'date_desc':
        events = events.order_by('-date')
    else:
        events = events.order_by('date')

    categories = Category.objects.all()

    return render(request, 'event_list.html', {
        'events': events,
        'categories': categories,
        'request_get': request.GET,
    })


def event_detail(request, pk):
    event = get_object_or_404(Event.objects.select_related('category', 'venue'), pk=pk)
    
    other_events = Event.objects.filter(
        category=event.category,
        is_active=True,
        date__gte=timezone.now()
    ).exclude(pk=event.pk).select_related('category', 'venue')[:4]

    return render(request, 'event_detail.html', {
        'event': event,
        'other_events': other_events,
    })


@staff_member_required
def event_create(request):
    if request.method == 'POST':
        form = EventForm(request.POST, request.FILES)
        if form.is_valid():
            event = form.save(commit=False)
            event.available_seats = event.total_seats
            event.save()
            messages.success(request, 'Событие успешно создано!')
            return redirect(event.get_absolute_url())
    else:
        form = EventForm()
    return render(request, 'event_form.html', {'form': form, 'title': 'Создать событие'})


@staff_member_required
def event_edit(request, pk):
    event = get_object_or_404(Event, pk=pk)
    if request.method == 'POST':
        form = EventForm(request.POST, request.FILES, instance=event)
        if form.is_valid():
            form.save()
            messages.success(request, 'Событие обновлено!')
            return redirect(event.get_absolute_url())
    else:
        form = EventForm(instance=event)
    return render(request, 'event_form.html', {'form': form, 'title': 'Редактировать событие', 'event': event})


@staff_member_required
def event_delete(request, pk):
    event = get_object_or_404(Event, pk=pk)
    if request.method == 'POST':
        event.delete()
        messages.success(request, 'Событие удалено.')
        return redirect('event_list')
    return render(request, 'event_confirm_delete.html', {'event': event})



@login_required
def book_ticket(request, pk):
    if request.method != 'POST':
        return redirect('event_detail', pk=pk)

    try:
        seats_count = int(request.POST.get('seats_count', 1))
    except ValueError:
        messages.error(request, 'Некорректное количество билетов.')
        return redirect('event_detail', pk=pk)

    if seats_count < 1 or seats_count > 10:
        messages.error(request, 'Можно забронировать от 1 до 10 билетов.')
        return redirect('event_detail', pk=pk)

    with transaction.atomic():
        event = get_object_or_404(Event.objects.select_for_update(), pk=pk)

        if not event.is_active or event.is_past:
            messages.error(request, 'Нельзя забронировать прошедшее или неактивное событие.')
            return redirect('event_detail', pk=pk)

        if event.available_seats < seats_count:
            messages.error(request, f'Недостаточно свободных мест. Доступно: {event.available_seats}')
            return redirect('event_detail', pk=pk)

        total_price = event.price * seats_count

        Booking.objects.create(
            user=request.user,
            event=event,
            seats_count=seats_count,
            total_price=total_price,
            status=Booking.STATUS_CONFIRMED
        )

        event.available_seats = F('available_seats') - seats_count
        event.save()

    messages.success(request, 'Билеты успешно забронированы!')
    return redirect('my_tickets')


@login_required
def cancel_booking(request, pk):
    booking = get_object_or_404(Booking, pk=pk, user=request.user)

    if booking.status == Booking.STATUS_CANCELLED:
        messages.error(request, 'Эта бронь уже отменена.')
        return redirect('my_tickets')

    if booking.event.date < timezone.now():
        messages.error(request, 'Нельзя отменить бронь на прошедшее событие.')
        return redirect('my_tickets')

    with transaction.atomic():
        booking.status = Booking.STATUS_CANCELLED
        booking.save()

        Event.objects.filter(pk=booking.event.pk).update(
            available_seats=F('available_seats') + booking.seats_count
        )

    messages.success(request, 'Бронирование успешно отменено.')
    return redirect('my_tickets')


@login_required
def my_tickets(request):
    user_bookings = Booking.objects.filter(user=request.user)\
                                   .select_related('event', 'event__venue', 'event__category')

    now = timezone.now()
    upcoming_bookings = user_bookings.filter(event__date__gte=now, status=Booking.STATUS_CONFIRMED)
    past_or_cancelled = user_bookings.filter(Q(event__date__lt=now) | Q(status=Booking.STATUS_CANCELLED))

    summary = user_bookings.filter(status=Booking.STATUS_CONFIRMED).aggregate(
        total_tickets=Coalesce(Sum('seats_count'), 0),
        total_spent=Coalesce(Sum('total_price'), Decimal('0.00'))
    )

    return render(request, 'my_tickets.html', {
        'upcoming_bookings': upcoming_bookings,
        'past_or_cancelled': past_or_cancelled,
        'summary': summary,
    })



def stats(request):
    top_events = Event.objects.annotate(
        tickets=Coalesce(Sum('bookings__seats_count', filter=Q(bookings__status='confirmed')), 0)
    ).order_by('-tickets')[:5]

    categories_stats = Category.objects.annotate(events_count=Count('events'))
    top_venues = Venue.objects.annotate(events_count=Count('events')).order_by('-events_count')

    total_stats = Event.objects.aggregate(
        total_events=Count('id'),
        total_tickets_sold=Coalesce(Sum('bookings__seats_count', filter=Q(bookings__status='confirmed')), 0),
        total_revenue=Coalesce(Sum('bookings__total_price', filter=Q(bookings__status='confirmed')), Decimal('0.00'))
    )

    return render(request, 'stats.html', {
        'top_events': top_events,
        'categories_stats': categories_stats,
        'top_venues': top_venues,
        'total_stats': total_stats,
    })



def register(request):
    if request.method == 'POST':
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.set_password(form.cleaned_data['password'])
            user.is_active = True  
            user.save()

            login(request, user)  
            messages.success(request, 'Вы успешно зарегистрировались!')
            return redirect('event_list')
    else:
        form = RegisterForm()
    return render(request, 'register.html', {'form': form})


@login_required
def profile(request):
    """Профиль пользователя"""
    if request.method == 'POST':
        form = ProfileForm(request.POST, request.FILES, instance=request.user.profile)
        if form.is_valid():
            form.save()
            messages.success(request, 'Профиль обновлен!')
            return redirect('profile')
    else:
        form = ProfileForm(instance=request.user.profile)
    return render(request, 'profile.html', {'form': form})