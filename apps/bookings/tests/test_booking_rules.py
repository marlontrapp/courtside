"""Tests for booking rules: links, every kind, member guests, enforcement, management API, availability."""

from datetime import date, datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.db.models import RestrictedError
from django.urls import reverse
from rest_framework import serializers, status
from rest_framework.test import APITestCase

from apps.bookings import services
from apps.bookings.models import BookingRule, Reservation
from apps.bookings.rules import RULE_KINDS, period_bounds
from apps.bookings.services import BookingError
from apps.common.tests.factories import (
    create_booking_rule,
    create_club,
    create_club_membership,
    create_club_staff_role,
    create_court,
    create_court_schedule,
    create_day_template,
    create_player_profile,
    create_reservation,
    create_schedule_override,
    create_user,
)
from apps.common.tests.utils import FrozenTimeMixin
from apps.courts.models import ClubMembership, ClubStaffRole, DayTemplateSlot
from apps.courts.schedule import grid_slots, resolve

SP = ZoneInfo("America/Sao_Paulo")
TODAY = date(2030, 1, 7)  # Monday; the week is Mon 7 – Sun 13
TUESDAY, WEDNESDAY, SUNDAY, NEXT_MONDAY = (TODAY + timedelta(days=n) for n in (1, 2, 6, 7))


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=SP)


def player(gender="", birth_date=None, **user_fields):
    user = create_user(**user_fields)
    create_player_profile(user=user, gender=gender, birth_date=birth_date)
    return user


class RulesTestCase(FrozenTimeMixin, APITestCase):
    """
    Two courts sharing two slot configurations: "Workdays" (08:00–22:00,
    hourly) Monday to Friday and "Weekend" (08:00–20:00) Saturday and Sunday.
    """

    frozen_now = at(TODAY, 9, 30)

    @classmethod
    def setUpTestData(cls):
        cls.club = create_club(name="Rules Club")
        cls.court = create_court(club=cls.club, name="Court 1")
        cls.court2 = create_court(club=cls.club, name="Court 2")
        cls.workdays = create_day_template(cls.club, grid_slots(time(8), time(22), 60), name="Workdays")
        cls.weekend = create_day_template(cls.club, grid_slots(time(8), time(20), 60), name="Weekend")
        week = {**{day: cls.workdays for day in range(5)}, 5: cls.weekend, 6: cls.weekend}
        for court in (cls.court, cls.court2):
            create_court_schedule(court, days=week)
        cls.ana = player("FEMALE", date(1990, 5, 20), first_name="Ana")
        cls.bia = player("FEMALE", date(1995, 1, 1), first_name="Bia")
        cls.caio = player("MALE", date(1980, 3, 3), first_name="Caio")
        cls.dani = player(first_name="Dani")  # no gender, no birth date

    @staticmethod
    def slot(template, hour: int) -> DayTemplateSlot:
        return template.slots.get(start_time=time(hour))

    def evening(self, template=None) -> list[DayTemplateSlot]:
        """18:00–22:00 ("prime time") slots of a template (Workdays by default)."""
        return [self.slot(template or self.workdays, hour) for hour in (18, 19, 20, 21)]

    def rule(self, kind, params=None, **links):
        return create_booking_rule(self.club, kind, params, **links)

    def book(self, user, day=TUESDAY, hour=18, *, court=None, hours=1, booking_type="SINGLES", guests=()):
        """Book through the booking service (rules enforced)."""
        return services.create_reservation(
            court=court or self.court, primary_player=user,
            start_datetime=at(day, hour), end_datetime=at(day, hour + hours),
            booking_type=booking_type, guests=list(guests), status=Reservation.Status.CONFIRMED,
        )

    def rejected(self, user, **kwargs) -> list[str]:
        with self.assertRaises(BookingError) as ctx:
            self.book(user, **kwargs)
        self.assertFalse(ctx.exception.conflict, "a slot conflict, not a rule")
        return ctx.exception.errors

    def covers(self, rule, court, day, start_hour, end_hour) -> bool:
        """Does `rule` cover a booking [start_hour, end_hour) on `court` on `day`, per the real schedule?"""
        rule = BookingRule.objects.prefetch_related("courts", "day_templates", "slots").get(pk=rule.pk)
        covered = resolve([court], day, day).covered(court.pk, day, time(start_hour), time(end_hour))
        return rule.covers(court.pk, *covered)


class LinkTests(RulesTestCase):
    def test_nothing_linked_covers_the_whole_club(self):
        rule = self.rule("booking_types")

        for court, day, hour in ((self.court, TUESDAY, 8), (self.court2, SUNDAY, 19)):
            self.assertTrue(self.covers(rule, court, day, hour, hour + 1))

    def test_courts(self):
        rule = self.rule("booking_types", courts=[self.court])

        self.assertTrue(self.covers(rule, self.court, TUESDAY, 8, 9))
        self.assertFalse(self.covers(rule, self.court2, TUESDAY, 8, 9))

    def test_slot_configuration(self):
        rule = self.rule("booking_types", day_templates=[self.weekend])

        self.assertTrue(self.covers(rule, self.court, SUNDAY, 10, 11))
        self.assertFalse(self.covers(rule, self.court, TUESDAY, 10, 11))  # Workdays that day

    def test_single_slots(self):
        rule = self.rule("booking_types", slots=[self.slot(self.workdays, 19)])

        self.assertTrue(self.covers(rule, self.court, TUESDAY, 19, 20))
        self.assertTrue(self.covers(rule, self.court2, TUESDAY, 18, 20))  # a two-slot booking including it
        self.assertFalse(self.covers(rule, self.court, TUESDAY, 18, 19))
        self.assertFalse(self.covers(rule, self.court, SUNDAY, 19, 20))  # same time, Weekend's own slot

    def test_links_narrow_each_other(self):
        rule = self.rule("booking_types", courts=[self.court], slots=self.evening())

        self.assertTrue(self.covers(rule, self.court, TUESDAY, 20, 21))
        self.assertFalse(self.covers(rule, self.court2, TUESDAY, 20, 21))
        self.assertFalse(self.covers(rule, self.court, TUESDAY, 10, 11))

    def test_a_one_time_override_changes_the_day_template(self):
        special = create_day_template(self.club, [(time(8), time(12))], name="Tournament morning")
        create_schedule_override(self.court, TUESDAY, special)
        workdays_rule = self.rule("booking_types", day_templates=[self.workdays])

        self.assertFalse(self.covers(workdays_rule, self.court, TUESDAY, 8, 9))
        self.assertTrue(self.covers(workdays_rule, self.court2, TUESDAY, 8, 9))

    def test_date_range_and_active_flag(self):
        rule = self.rule("booking_types", valid_from=TUESDAY, valid_until=WEDNESDAY)

        self.assertEqual([rule.applies_on(d) for d in (TODAY, TUESDAY, WEDNESDAY, SUNDAY)], [False, True, True, False])
        rule.is_active = False
        self.assertFalse(rule.applies_on(TUESDAY))

    def test_database_rejects_a_reversed_date_range(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            BookingRule.objects.create(club=self.club, name="x", kind="booking_types", params={},
                                       valid_from=WEDNESDAY, valid_until=TUESDAY)


class LinkProtectionTests(RulesTestCase):
    """A linked court / template / slot can't disappear: the rule would silently cover the whole club."""

    def test_a_linked_court_cant_be_deleted(self):
        self.rule("booking_types", courts=[self.court])

        with self.assertRaises(RestrictedError):
            self.court.delete()

    def test_deleting_the_club_still_works(self):
        self.rule("booking_types", courts=[self.court], day_templates=[self.workdays], slots=self.evening())

        self.club.delete()

        self.assertFalse(BookingRule.objects.exists())


class BookingTypesRuleTests(RulesTestCase):
    def test_doubles_only_evening(self):
        self.rule("booking_types", {"allowed": ["DOUBLES"]}, name="Doubles evening", slots=self.evening())

        self.assertEqual(
            self.rejected(self.ana, hour=18, booking_type="SINGLES"),
            ["Doubles evening: doubles only (this is a singles booking)."],
        )
        self.book(self.ana, hour=18, booking_type="DOUBLES")
        self.book(self.ana, hour=10, booking_type="SINGLES")  # not a linked slot

    def test_a_court_linked_rule_applies_every_day_every_slot(self):
        self.rule("booking_types", {"allowed": ["LESSON"]}, name="Lessons court", courts=[self.court])

        self.book(self.ana, hour=9, booking_type="LESSON")
        self.assertEqual(
            self.rejected(self.ana, hour=21, booking_type="DOUBLES"),
            ["Lessons court: lesson only (this is a doubles booking)."],
        )
        self.book(self.ana, day=SUNDAY, hour=10, booking_type="LESSON")
        self.book(self.ana, hour=21, court=self.court2, booking_type="DOUBLES")  # other courts: no rule


class PlayerGenderRuleTests(RulesTestCase):
    def setUp(self):
        super().setUp()
        self.rule("player_gender", {"allowed": ["FEMALE"]}, name="Women's evening", slots=self.evening())

    def test_women_book_with_women(self):
        self.book(self.ana, guests=[self.bia])

    def test_men_cant_book(self):
        self.assertEqual(self.rejected(self.caio), ["Women's evening: women only."])

    def test_booker_without_gender_is_told_to_add_it(self):
        self.assertEqual(
            self.rejected(self.dani),
            ["Women's evening: women only — add your gender to your profile if this applies to you."],
        )
        self.assertEqual(self.rejected(create_user()), self.rejected(self.dani))  # no profile at all

    def test_a_guest_not_meeting_it_is_not_identified(self):
        for guest in (self.caio, self.dani):
            with self.subTest(guest.first_name):
                errors = self.rejected(self.ana, guests=[self.bia, guest])

                self.assertEqual(errors, ["Women's evening: women only — at least one of your guests doesn't meet it."])
                self.assertNotIn(guest.first_name, errors[0])
                self.assertNotIn(guest.email, errors[0])

    def test_applies_to_the_booker_only(self):
        BookingRule.objects.update(params={"allowed": ["FEMALE"], "applies_to": "booker"})

        self.book(self.ana, guests=[self.caio])

    def test_outside_the_linked_slots(self):
        self.book(self.caio, hour=10)
        self.book(self.caio, day=SUNDAY, hour=18)  # Weekend's 18:00 isn't linked


class PlayerAgeRuleTests(RulesTestCase):
    def test_minimum_age_on_the_match_day(self):
        self.rule("player_age", {"min_age": 40}, name="Seniors")
        turns_40_on_tuesday = player(birth_date=date(1990, 1, 8))
        turns_40_on_wednesday = player(birth_date=date(1990, 1, 9))

        self.book(turns_40_on_tuesday, day=TUESDAY)  # 39 today, 40 on the match day
        self.assertEqual(self.rejected(turns_40_on_wednesday, day=TUESDAY, hour=19), ["Seniors: ages 40+."])

    def test_age_range(self):
        self.rule("player_age", {"min_age": 12, "max_age": 17}, name="Juniors")
        junior, adult = player(birth_date=date(2015, 1, 1)), player(birth_date=date(2000, 1, 1))

        self.book(junior)
        self.assertEqual(self.rejected(adult, hour=19), ["Juniors: ages 12–17."])

    def test_missing_birth_date(self):
        self.rule("player_age", {"max_age": 17}, name="Juniors")

        self.assertEqual(self.rejected(self.dani), ["Juniors: ages up to 17 — add your birth date to your profile."])

    def test_guests_are_checked_without_revealing_their_age(self):
        self.rule("player_age", {"min_age": 40}, name="Seniors")

        self.book(self.caio, guests=[player(birth_date=date(1950, 1, 1))])
        errors = self.rejected(self.caio, hour=19, guests=[self.ana])
        self.assertEqual(errors, ["Seniors: ages 40+ — at least one of your guests doesn't meet it."])


class MaxBookingsPerPeriodTests(RulesTestCase):
    def test_weekly_limit_club_wide(self):
        self.rule("max_bookings_per_period", {"max": 2, "period": "week"}, name="Fair play")
        self.book(self.ana, day=TUESDAY)
        self.book(self.ana, day=SUNDAY, court=self.court2)

        self.assertEqual(
            self.rejected(self.ana, day=WEDNESDAY),
            ["Fair play: max 2 bookings per week — you already have 2 bookings that week."],
        )
        self.book(self.ana, day=NEXT_MONDAY)  # a new week
        self.book(self.bia, day=WEDNESDAY)  # other players have their own count

    def test_what_counts(self):
        self.rule("max_bookings_per_period", {"max": 1, "period": "week"}, name="One a week")
        cancelled = self.book(self.ana, day=TUESDAY)
        Reservation.objects.filter(pk=cancelled.pk).update(status=Reservation.Status.CANCELLED)
        create_reservation(court=create_court(), primary_player=self.ana,  # another club: doesn't count
                           start_datetime=at(TUESDAY, 12), end_datetime=at(TUESDAY, 13))

        self.book(self.ana, day=WEDNESDAY)  # nothing counted so far

        Reservation.objects.filter(primary_player=self.ana, court__club=self.club, status="CONFIRMED").update(
            status=Reservation.Status.COMPLETED
        )
        self.assertTrue(self.rejected(self.ana, day=SUNDAY))  # completed bookings count

    def test_periods(self):
        self.assertEqual(period_bounds(WEDNESDAY, "day"), (WEDNESDAY, WEDNESDAY))
        self.assertEqual(period_bounds(WEDNESDAY, "week"), (TODAY, SUNDAY))
        self.assertEqual(period_bounds(SUNDAY, "week"), (TODAY, SUNDAY))
        self.assertEqual(period_bounds(date(2030, 2, 14), "month"), (date(2030, 2, 1), date(2030, 2, 28)))

    def test_daily_limit(self):
        self.rule("max_bookings_per_period", {"max": 1, "period": "day"}, name="One a day")
        self.book(self.ana, hour=8)

        self.assertTrue(self.rejected(self.ana, hour=20, court=self.court2))
        self.book(self.ana, day=WEDNESDAY)

    def test_prime_time_counts_only_prime_time_bookings(self):
        # "Players with fewer than 1 prime-time booking this week may book prime time."
        self.rule("max_bookings_per_period", {"max": 1, "period": "week"}, name="Prime time", slots=self.evening())
        self.book(self.ana, day=TUESDAY, hour=10)  # morning: not counted

        self.book(self.ana, day=TUESDAY, hour=19)
        self.assertEqual(
            self.rejected(self.ana, day=WEDNESDAY, hour=20),
            ["Prime time: max 1 booking per week — you already have 1 booking that week."],
        )
        self.book(self.ana, day=WEDNESDAY, hour=9)  # not a prime-time slot: the rule doesn't apply

    def test_prime_time_counting_every_club_booking(self):
        self.rule("max_bookings_per_period", {"max": 1, "period": "week", "counts": "club"}, name="Prime time",
                  slots=self.evening())
        self.book(self.ana, day=TUESDAY, hour=10)

        self.assertEqual(
            self.rejected(self.ana, day=WEDNESDAY, hour=20),
            ["Prime time: max 1 booking per week at the club — you already have 1 booking that week."],
        )


class MaxActiveBookingsTests(RulesTestCase):
    def test_one_upcoming_booking(self):
        self.rule("max_active_bookings", {"max": 1}, name="One at a time")
        first = self.book(self.ana, day=TUESDAY)

        self.assertEqual(
            self.rejected(self.ana, day=WEDNESDAY),
            ["One at a time: max 1 upcoming booking — you already have 1."],
        )
        Reservation.objects.filter(pk=first.pk).update(status=Reservation.Status.CANCELLED)
        self.book(self.ana, day=WEDNESDAY)

    def test_finished_bookings_dont_count(self):
        self.rule("max_active_bookings", {"max": 1})
        create_reservation(court=self.court, primary_player=self.ana,
                           start_datetime=at(TODAY, 8), end_datetime=at(TODAY, 9))  # ended at 9:00; now is 9:30

        self.book(self.ana)

    def test_a_match_in_progress_counts(self):
        self.rule("max_active_bookings", {"max": 1})
        create_reservation(court=self.court, primary_player=self.ana,
                           start_datetime=at(TODAY, 9), end_datetime=at(TODAY, 10))

        self.assertTrue(self.rejected(self.ana))


class MemberGuestTests(RulesTestCase):
    """A guest who is a member of the club: the booking counts towards their limits too."""

    def member(self, user, **extra):
        return create_club_membership(user=user, club=self.club, **extra)

    def as_guest(self, guest, day=TUESDAY, hour=10, booker=None):
        booking = create_reservation(court=self.court2, primary_player=booker or self.caio,
                                     start_datetime=at(day, hour), end_datetime=at(day, hour + 1))
        booking.guests.set([guest])
        return booking

    def test_a_members_bookings_as_guest_count_when_they_book(self):
        self.rule("max_bookings_per_period", {"max": 1, "period": "week"}, name="One a week")
        self.member(self.ana)
        self.as_guest(self.ana, day=TUESDAY)

        self.assertEqual(
            self.rejected(self.ana, day=WEDNESDAY),
            ["One a week: max 1 booking per week — you already have 1 booking that week."],
        )

    def test_a_non_members_bookings_as_guest_dont_count(self):
        self.rule("max_bookings_per_period", {"max": 1, "period": "week"})
        self.as_guest(self.ana, day=TUESDAY)

        self.book(self.ana, day=WEDNESDAY)

    def test_a_member_guest_at_their_limit_blocks_the_booking(self):
        self.rule("max_active_bookings", {"max": 1}, name="One at a time")
        self.member(self.bia)
        self.book(self.bia, day=WEDNESDAY)  # Bia's one upcoming booking

        errors = self.rejected(self.ana, guests=[self.bia])

        self.assertEqual(errors, ["One at a time: max 1 upcoming booking — at least one of your guests has already reached it."])
        self.assertNotIn("Bia", errors[0])

    def test_a_member_guest_at_their_limit_by_guest_bookings_blocks_it_too(self):
        self.rule("max_bookings_per_period", {"max": 1, "period": "week"})
        self.member(self.bia)
        self.as_guest(self.bia, day=TUESDAY, hour=10)

        self.assertTrue(self.rejected(self.ana, day=WEDNESDAY, guests=[self.bia]))

    def test_a_non_member_guest_isnt_limited(self):
        self.rule("max_active_bookings", {"max": 1})
        self.book(self.bia, day=WEDNESDAY)  # Bia isn't a member

        self.book(self.ana, guests=[self.bia])

    def test_suspended_or_expired_memberships_dont_count(self):
        self.rule("max_active_bookings", {"max": 1})
        self.book(self.bia, day=WEDNESDAY)
        for booker, extra in (
            (self.ana, {"status": ClubMembership.Status.SUSPENDED}),
            (self.caio, {"expires_at": at(TODAY, 8)}),  # expired this morning
        ):
            with self.subTest(extra):
                membership = self.member(self.bia, **extra)

                self.book(booker, court=self.court if booker == self.ana else self.court2, guests=[self.bia])
                membership.delete()

    def test_a_booking_counts_once_for_a_member_booked_and_guest_in_it(self):
        self.rule("max_bookings_per_period", {"max": 2, "period": "week"})
        self.member(self.ana)
        self.as_guest(self.ana, booker=self.ana, day=TUESDAY)  # odd but possible: counted once

        self.book(self.ana, day=WEDNESDAY)

    def test_the_counted_players_are_locked(self):
        self.rule("max_active_bookings", {"max": 3})
        self.member(self.bia)

        with mock.patch("apps.bookings.rules.lock_players") as lock:
            self.book(self.ana, guests=[self.bia, self.caio])  # Caio isn't a member

        lock.assert_called_once()
        self.assertEqual(sorted(u.first_name for u in lock.call_args.args[0]), ["Ana", "Bia"])


class EnforcementTests(RulesTestCase):
    def test_every_broken_rule_is_reported(self):
        self.rule("booking_types", {"allowed": ["DOUBLES"]}, name="Doubles")
        self.rule("player_gender", {"allowed": ["FEMALE"]}, name="Women")

        self.assertEqual(
            sorted(self.rejected(self.caio)), ["Doubles: doubles only (this is a singles booking).", "Women: women only."]
        )

    def test_other_clubs_and_inactive_rules_are_ignored(self):
        create_booking_rule(create_club(), "player_gender", {"allowed": ["FEMALE"]})
        self.rule("player_gender", {"allowed": ["FEMALE"]}, is_active=False)

        self.book(self.caio)

    def test_staff_tools_can_skip_rules(self):
        self.rule("player_gender", {"allowed": ["FEMALE"]})

        booking = services.create_reservation(
            court=self.court, primary_player=self.caio, start_datetime=at(TUESDAY, 18),
            end_datetime=at(TUESDAY, 19), enforce_rules=False,
        )
        self.assertEqual(booking.primary_player, self.caio)

    def test_an_invalid_slot_is_reported_before_rules(self):
        self.rule("player_gender", {"allowed": ["FEMALE"]}, name="Women")

        errors = self.rejected(self.caio, hour=7)  # the court opens at 8
        self.assertFalse(any("Women" in error for error in errors))

    def test_only_count_rules_lock_players(self):
        self.rule("player_gender", {"allowed": ["FEMALE"]})
        with mock.patch("apps.bookings.rules.lock_players") as lock:
            self.book(self.ana)
        lock.assert_not_called()

        self.rule("max_active_bookings", {"max": 5})
        with mock.patch("apps.bookings.rules.lock_players") as lock:
            self.book(self.ana, day=WEDNESDAY)
        lock.assert_called_once_with([self.ana])


class ParamsValidationTests(RulesTestCase):
    def invalid(self, kind, params):
        with self.assertRaises(serializers.ValidationError) as ctx:
            RULE_KINDS[kind].validate_params(params)
        return ctx.exception.detail

    def test_invalid_params(self):
        for kind, params in (
            ("booking_types", {}),
            ("booking_types", {"allowed": []}),
            ("booking_types", {"allowed": ["SQUASH"]}),
            ("player_gender", {"allowed": ["PREFER_NOT_TO_SAY"]}),
            ("player_gender", {"allowed": ["FEMALE"], "applies_to": "everyone"}),
            ("player_age", {}),
            ("player_age", {"min_age": 18, "max_age": 12}),
            ("player_age", {"min_age": -1}),
            ("max_bookings_per_period", {"max": 0, "period": "week"}),
            ("max_bookings_per_period", {"max": 1, "period": "year"}),
            ("max_active_bookings", {"max": 1, "counts": "all"}),
            ("max_active_bookings", ["max", 1]),
        ):
            with self.subTest(kind=kind, params=params):
                self.invalid(kind, params)

    def test_normalized_with_defaults(self):
        self.assertEqual(RULE_KINDS["booking_types"].validate_params({"allowed": ["DOUBLES", "DOUBLES"]}),
                         {"allowed": ["DOUBLES"]})
        self.assertEqual(RULE_KINDS["player_gender"].validate_params({"allowed": ["FEMALE"]}),
                         {"allowed": ["FEMALE"], "applies_to": "all_players"})
        self.assertEqual(RULE_KINDS["player_age"].validate_params({"min_age": 60}),
                         {"min_age": 60, "max_age": None, "applies_to": "all_players"})
        self.assertEqual(RULE_KINDS["max_active_bookings"].validate_params({"max": 1}),
                         {"max": 1, "counts": "matching"})


# --- API ---------------------------------------------------------------------------


class BookingAPITests(RulesTestCase):
    def test_rule_violations_are_a_400(self):
        self.rule("player_gender", {"allowed": ["FEMALE"]}, name="Women's evening", slots=self.evening())
        self.client.force_authenticate(self.caio)

        response = self.client.post(reverse("bookings:reservation-list"), {
            "court": str(self.court.pk), "date": TUESDAY.isoformat(), "start_time": "18:00", "end_time": "19:00",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data, {"non_field_errors": ["Women's evening: women only."]})
        self.assertFalse(Reservation.objects.exists())


class BookingRuleAPITests(RulesTestCase):
    def setUp(self):
        super().setUp()
        self.admin = create_user()
        create_club_staff_role(user=self.admin, club=self.club, role=ClubStaffRole.Role.ADMIN, start_date=TODAY)
        self.client.force_authenticate(self.admin)
        self.url = reverse("bookings:booking-rule-list", args=[self.club.pk])

    def detail(self, rule_id):
        return reverse("bookings:booking-rule-detail", args=[self.club.pk, rule_id])

    def template_url(self, template):
        return reverse("courts:day-template-detail", args=[self.club.pk, template.pk])

    def test_create_and_read(self):
        slot_ids = [str(s.pk) for s in self.evening()[:2]]

        response = self.client.post(self.url, {
            "name": "Women's evening", "kind": "player_gender", "params": {"allowed": ["FEMALE"]},
            "courts": [str(self.court.pk)], "day_templates": [str(self.workdays.pk)], "slots": slot_ids,
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        rule = BookingRule.objects.get()
        self.assertEqual(response.data, {
            "id": str(rule.pk), "name": "Women's evening", "kind": "player_gender",
            "params": {"allowed": ["FEMALE"], "applies_to": "all_players"}, "summary": "Women only",
            "courts": [self.court.pk], "day_templates": [self.workdays.pk],
            "slots": [s.pk for s in self.evening()[:2]], "valid_from": None, "valid_until": None,
            "is_active": True, "created_at": response.data["created_at"], "updated_at": response.data["updated_at"],
        })
        self.assertEqual(self.client.get(self.url).data, [self.client.get(self.detail(rule.pk)).data])

    def test_slot_ids_come_from_the_day_template_endpoint(self):
        template = self.client.get(self.template_url(self.workdays)).data

        self.assertEqual(template["slots"][0], {"id": str(self.slot(self.workdays, 8).pk), "start": "08:00", "end": "09:00"})

    def test_validation(self):
        other = create_club()
        foreign_template = create_day_template(other)
        payload = {"name": "x", "kind": "booking_types", "params": {"allowed": ["DOUBLES"]}}
        for extra, field in (
            ({"kind": "nope"}, "kind"),
            ({"kind": None}, "kind"),
            ({"kind": "player_age", "params": {}}, "params"),
            ({"valid_from": "2030-02-01", "valid_until": "2030-01-01"}, "valid_until"),
            ({"courts": [str(create_court(club=other).pk)]}, "courts"),
            ({"day_templates": [str(foreign_template.pk)]}, "day_templates"),
            ({"slots": [str(foreign_template.slots.first().pk)]}, "slots"),
            ({"day_templates": [str(self.weekend.pk)], "slots": [str(self.slot(self.workdays, 18).pk)]}, "slots"),
        ):
            with self.subTest(extra):
                response = self.client.post(self.url, {**payload, **extra}, format="json")

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(field, response.data)
        self.assertFalse(BookingRule.objects.exists())

    def test_patch(self):
        rule = self.rule("booking_types", {"allowed": ["DOUBLES"]}, slots=self.evening())

        paused = self.client.patch(self.detail(rule.pk), {"is_active": False}, format="json")
        self.assertEqual(paused.status_code, status.HTTP_200_OK, paused.data)
        self.assertFalse(paused.data["is_active"])

        bad = self.client.patch(self.detail(rule.pk), {"kind": "player_age"}, format="json")  # params don't fit
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("params", bad.data)

        changed = self.client.patch(self.detail(rule.pk), {"kind": "player_age", "params": {"min_age": 60}}, format="json")
        self.assertEqual(changed.data["summary"], "Ages 60+")

        contradiction = self.client.patch(self.detail(rule.pk), {"day_templates": [str(self.weekend.pk)]}, format="json")
        self.assertEqual(contradiction.status_code, status.HTTP_400_BAD_REQUEST)  # its slots are Workdays'
        relinked = self.client.patch(self.detail(rule.pk), {"slots": [], "courts": [str(self.court2.pk)]}, format="json")
        self.assertEqual((relinked.data["slots"], relinked.data["courts"]), ([], [self.court2.pk]))

    def test_delete_leaves_bookings_alone(self):
        rule = self.rule("max_active_bookings", {"max": 1})
        booking = self.book(self.ana)

        self.assertEqual(self.client.delete(self.detail(rule.pk)).status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(BookingRule.objects.exists())
        self.assertEqual(Reservation.objects.get(pk=booking.pk).status, Reservation.Status.CONFIRMED)

    def test_linked_slots_and_templates_are_protected(self):
        spare = create_day_template(self.club, grid_slots(time(8), time(12), 60), name="Spare")
        self.rule("booking_types", name="Spare mornings", slots=[self.slot(spare, 9)])
        keep_ids = {s.start_time: s.pk for s in spare.slots.all()}

        removing = self.client.patch(self.template_url(spare), {"slots": [
            {"start": "08:00", "end": "09:00"}, {"start": "10:00", "end": "12:00"}]}, format="json")
        self.assertEqual(removing.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("09:00–10:00", removing.data["detail"])
        self.assertIn("Spare mornings", removing.data["detail"])

        adding = self.client.patch(self.template_url(spare), {"slots": [  # 09:00 stays: allowed, ids kept
            {"start": "08:00", "end": "09:00"}, {"start": "09:00", "end": "10:00"}, {"start": "10:00", "end": "11:30"}]},
            format="json")
        self.assertEqual(adding.status_code, status.HTTP_200_OK, adding.data)
        self.assertEqual(spare.slots.get(start_time=time(9)).pk, keep_ids[time(9)])
        self.assertEqual(BookingRule.objects.get().slots.get().pk, keep_ids[time(9)])

        deleting = self.client.delete(self.template_url(spare))
        self.assertEqual(deleting.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("Spare mornings", deleting.data["detail"])

    def test_only_club_admins_and_staff(self):
        rule = self.rule("booking_types")
        for user in (create_user(), self.ana):
            self.client.force_authenticate(user)
            with self.subTest(user.email):
                self.assertEqual(self.client.get(self.url).status_code, status.HTTP_404_NOT_FOUND)
                self.assertEqual(self.client.get(self.detail(rule.pk)).status_code, status.HTTP_404_NOT_FOUND)
                self.assertEqual(self.client.post(self.url, {}, format="json").status_code, status.HTTP_404_NOT_FOUND)
        self.client.force_authenticate(create_user(is_staff=True))
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_200_OK)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN)

    def test_methods(self):
        rule = self.rule("booking_types")

        self.assertEqual(self.client.post(self.detail(rule.pk), {}, format="json").status_code,
                         status.HTTP_405_METHOD_NOT_ALLOWED)  # not a create on the detail URL
        for method in ("put", "patch", "delete"):
            with self.subTest(method):
                self.assertEqual(getattr(self.client, method)(self.url, {}, format="json").status_code,
                                 status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_rules_of_another_club_are_not_found(self):
        foreign = create_booking_rule(create_club())

        self.assertEqual(self.client.get(self.detail(foreign.pk)).status_code, status.HTTP_404_NOT_FOUND)

    def test_kinds(self):
        self.client.force_authenticate(self.ana)  # any logged-in user

        kinds = {k["kind"]: k for k in self.client.get(reverse("bookings:booking-rule-kinds")).data}

        self.assertEqual(set(kinds), set(RULE_KINDS))
        period = kinds["max_bookings_per_period"]["params"]
        self.assertEqual(period["max"], {"required": True, "type": "integer", "min": 1, "help": "Bookings allowed per period."})
        self.assertEqual(period["counts"]["default"], "matching")
        self.assertEqual(list(period["period"]["choices"]), ["day", "week", "month"])
        self.assertEqual(kinds["player_gender"]["params"]["allowed"]["type"], "list")


class AvailabilityTests(RulesTestCase):
    def test_slots_list_the_rules_that_cover_them(self):
        everywhere = self.rule("max_active_bookings", {"max": 1}, name="One at a time")
        evening = self.rule("player_gender", {"allowed": ["FEMALE"]}, name="Women's evening",
                            courts=[self.court], slots=[self.slot(self.workdays, 19), self.slot(self.workdays, 20)])
        self.client.force_authenticate(self.caio)

        response = self.client.get(reverse("courts:court-list"), {"date": TUESDAY.isoformat()})

        courts = {c["name"]: c for c in response.data["results"]}
        slots = {s["start"][11:16]: s["rules"] for s in courts["Court 1"]["available_slots"]}
        self.assertEqual(slots["18:00"], [str(everywhere.pk)])
        self.assertEqual(sorted(slots["19:00"]), sorted([str(everywhere.pk), str(evening.pk)]))
        self.assertEqual(slots["21:00"], [str(everywhere.pk)])
        self.assertEqual(
            sorted(courts["Court 1"]["booking_rules"], key=lambda r: r["name"]),
            [{"id": str(everywhere.pk), "name": "One at a time", "kind": "max_active_bookings",
              "summary": "Max 1 upcoming booking"},
             {"id": str(evening.pk), "name": "Women's evening", "kind": "player_gender", "summary": "Women only"}],
        )
        self.assertEqual([r["name"] for r in courts["Court 2"]["booking_rules"]], ["One at a time"])
