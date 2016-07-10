import logging
from warnings import warn

from django.db import models
from django.utils.functional import cached_property
from django.core.exceptions import ObjectDoesNotExist

from participants.models import Team
from venues.conflicts import venue_conflicts

from .generator import DRAW_FLAG_DESCRIPTIONS

logger = logging.getLogger(__name__)


class DebateManager(models.Manager):
    use_for_related_fields = True

    def get_queryset(self):
        return super().get_queryset().select_related('round')


class Debate(models.Model):
    STATUS_NONE = 'N'
    STATUS_POSTPONED = 'P'
    STATUS_DRAFT = 'D'
    STATUS_CONFIRMED = 'C'
    STATUS_CHOICES = ((STATUS_NONE, 'None'),
                      (STATUS_POSTPONED, 'Postponed'),
                      (STATUS_DRAFT, 'Draft'),
                      (STATUS_CONFIRMED, 'Confirmed'), )

    objects = DebateManager()

    round = models.ForeignKey('tournaments.Round', db_index=True)
    venue = models.ForeignKey('venues.Venue', blank=True, null=True)
    division = models.ForeignKey('divisions.Division', blank=True, null=True)

    bracket = models.FloatField(default=0)
    room_rank = models.IntegerField(default=0)

    time = models.DateTimeField(
        blank=True, null=True,
        help_text="The time/date of a debate if it is specifically scheduled")

    # comma-separated list of strings
    flags = models.CharField(max_length=100, blank=True, null=True)

    importance = models.IntegerField(default=0)
    result_status = models.CharField(max_length=1,
                                     choices=STATUS_CHOICES,
                                     default=STATUS_NONE)
    ballot_in = models.BooleanField(default=False)

    def __contains__(self, team):
        return team in (self.aff_team, self.neg_team)

    def __str__(self):
        try:
            return "[{}/{}] {} vs {}".format(
                self.round.tournament.slug, self.round.abbreviation,
                self.aff_team.short_name, self.neg_team.short_name)
        except DebateTeam.DoesNotExist:
            return "[{}/{}] {}".format(
                self.round.tournament.slug, self.round.abbreviation,
                ", ".join([x.short_name for x in self.teams]))

    @property
    def teams(self):
        return Team.objects.filter(debateteam__debate=self)

    @property
    def aff_team(self):
        try:
            return self._aff_team # may be populated by Round.debate_set_with_prefetches
        except AttributeError:
            self._aff_team = Team.objects.select_related('institution').get(
                debateteam__debate=self, debateteam__position=DebateTeam.POSITION_AFFIRMATIVE)
            return self._aff_team

    @property
    def neg_team(self):
        try:
            return self._neg_team
        except AttributeError:
            self._neg_team = Team.objects.select_related('institution').get(
                debateteam__debate=self, debateteam__position=DebateTeam.POSITION_NEGATIVE)
            return self._neg_team

    @property
    def aff_dt(self):
        try:
            return self._aff_dt # may be populated by Round.debate_set_with_prefetches
        except AttributeError:
            self._aff_dt = self.debateteam_set.select_related('team', 'team__institution').get(
                position=DebateTeam.POSITION_AFFIRMATIVE)
            return self._aff_dt

    @property
    def neg_dt(self):
        try:
            return self._neg_dt # may be populated by Round.debate_set_with_prefetches
        except AttributeError:
            self._neg_dt = self.debateteam_set.select_related('team', 'team__institution').get(
                position=DebateTeam.POSITION_NEGATIVE)
            return self._neg_dt # may be populated by Round.debate_set_with_prefetches

    def get_team(self, side):
        return getattr(self, '%s_team' % side)

    def get_dt(self, side):
        """dt = DebateTeam"""
        return getattr(self, '%s_dt' % side)

    def get_side(self, team):
        if self.aff_team == team:
            return 'aff'
        if self.neg_team == team:
            return 'neg'
        return None

    @cached_property
    def confirmed_ballot(self):
        """Returns the confirmed BallotSubmission for this debate, or None if
        there is no such ballot submission."""
        try:
            return self.ballotsubmission_set.get(confirmed=True)
        except ObjectDoesNotExist:  # BallotSubmission isn't defined yet, so can't use BallotSubmission.DoesNotExist
            return None

    @property
    def identical_ballotsubs_dict(self):
        """Returns a dict. Keys are BallotSubmissions, values are lists of
        version numbers of BallotSubmissions that are identical to the key's
        BallotSubmission. Excludes discarded ballots (always)."""
        ballotsubs = self.ballotsubmission_set.exclude(discarded=True).order_by('version')
        result = {b: list() for b in ballotsubs}
        for ballotsub1 in ballotsubs:
            # Save a bit of time by avoiding comparisons already done.
            # This relies on ballots being ordered by version.
            for ballotsub2 in ballotsubs.filter(
                    version__gt=ballotsub1.version):
                if ballotsub1.is_identical(ballotsub2):
                    result[ballotsub1].append(ballotsub2.version)
                    result[ballotsub2].append(ballotsub1.version)
        for l in result.values():
            l.sort()
        return result

    @property
    def flags_all(self):
        if not self.flags:
            return []
        else:
            return [DRAW_FLAG_DESCRIPTIONS[f] for f in self.flags.split(",")]

    @property
    def all_conflicts(self):
        return self.draw_conflicts + self.adjudicator_conflicts + venue_conflicts(self)

    @cached_property
    def draw_conflicts(self):
        d = []
        history = self.aff_team.seen(self.neg_team, before_round=self.round.seq)
        if history == 1:
            d.append("Teams have met once")
        elif history == 2:
            d.append("Teams have met twice")
        elif history > 2:
            d.append("Teams have met %d times" % (history,))
        if self.aff_team.institution_id == self.neg_team.institution_id:
            d.append("Teams are from the same institution")

        return d

    @cached_property
    def adjudicator_conflicts(self):
        class Conflict(object):
            def __init__(self, adj, team):
                self.adj = adj
                self.team = team

            def __str__(self):
                return 'Adjudicator %s conflicts with %s' % (self.adj, self.team)

        a = []
        for t, adj in self.adjudicators:
            for team in (self.aff_team, self.neg_team):
                if adj.conflict_with(team):
                    a.append(Conflict(adj, team))

        return a

    @property
    def adjudicators(self):
        """Returns an AdjudicatorAllocation containing the adjudicators for this
        debate."""
        try:
            return self._adjudicators
        except AttributeError:
            from adjallocation.allocation import AdjudicatorAllocation
            self._adjudicators = AdjudicatorAllocation(self, from_db=True)
            return self._adjudicators

    @property
    def chair(self):
        # Deprecated 4/7/2016, remove completely after 4/8/2016
        raise RuntimeError("Debate.chair is deprecated, use Debate.adjudicators.chair instead")
        # from adjallocation.models import DebateAdjudicator
        # da_adj = list(DebateAdjudicator.objects.filter(debate=self, type="C"))
        # a_adj = da_adj[0].adjudicator
        # return a_adj

    @property
    def matchup(self):
        return '%s vs %s' % (self.aff_team.short_name,
                             self.neg_team.short_name)

    @property
    def division_motion(self):
        from motions.models import Motion
        return Motion.objects.filter(round=self.round, divisions=self.division)


class DebateTeamManager(models.Manager):
    use_for_related_fields = True

    def get_queryset(self):
        return super().get_queryset().select_related('debate')


class DebateTeam(models.Model):
    POSITION_AFFIRMATIVE = 'A'
    POSITION_NEGATIVE = 'N'
    POSITION_UNALLOCATED = 'u'
    POSITION_CHOICES = ((POSITION_AFFIRMATIVE, 'Affirmative'),
                        (POSITION_NEGATIVE, 'Negative'),
                        (POSITION_UNALLOCATED, 'Unallocated'), )

    objects = DebateTeamManager()

    debate = models.ForeignKey(Debate, db_index=True)
    team = models.ForeignKey('participants.Team')
    position = models.CharField(max_length=1, choices=POSITION_CHOICES)

    def __str__(self):
        return '{} in {}'.format(self.team.short_name, self.debate)

    @cached_property
    def opposition(self):
        try:
            return DebateTeam.objects.exclude(position=self.position).select_related(
                    'team', 'team__institution').get(debate=self.debate)
        except (DebateTeam.DoesNotExist, DebateTeam.MultipleObjectsReturned):
            logger.error("Error finding opposition: %s, %s", self.debate, self.position)
            return None

    @property
    def result(self):
        # Added 4/7/2016, remove after 4/8/2016
        warn("DebateTeam.result is deprecated, use DebateTeam.get_result_display() instead.", stacklevel=2)

        if self.win is True:
            return 'won'
        elif self.win is False:
            return 'lost'
        else:
            return 'result unknown'

    @property
    def win(self):
        """Convenience function. Returns True if this team won, False if this
        team lost, or None if there isn't a confirmed result.

        This result is stored for the lifetime of the instance -- it won't
        update on the same instance if a result is entered."""
        try:
            return self._win
        except AttributeError:
            try:
                self._win = self.teamscore_set.get(ballot_submission__confirmed=True).win
            except ObjectDoesNotExist:
                self._win = None
            return self._win


class TeamPositionAllocation(models.Model):
    """Model to store team position allocations for tournaments like Joynt
    Scroll (New Zealand). Each team-round combination should have one of these.
    In tournaments without team position allocations, just don't use this
    model."""

    POSITION_AFFIRMATIVE = DebateTeam.POSITION_AFFIRMATIVE
    POSITION_NEGATIVE = DebateTeam.POSITION_NEGATIVE
    POSITION_UNALLOCATED = DebateTeam.POSITION_UNALLOCATED
    POSITION_CHOICES = DebateTeam.POSITION_CHOICES

    round = models.ForeignKey('tournaments.Round')
    team = models.ForeignKey('participants.Team')
    position = models.CharField(max_length=1, choices=POSITION_CHOICES)

    class Meta:
        unique_together = [('round', 'team')]
