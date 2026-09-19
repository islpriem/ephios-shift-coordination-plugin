# Feature tour

Screenshots from the development stack with its synthetic demo data.

## Members answer on their phone

One row per service, one column group per shift they may take, four choices and an optional
offer to sit in. The bottom row sets a whole column at once, and the footer says what is still
open.

<img src="images/survey-phone.png" alt="The availability survey on a phone" width="360">

## The answers arrive

The period page counts who answered, names the recommended personal maximum and says plainly
whether the shifts can be filled with it. Planning can start before the deadline; closing the
survey early takes one confirmation.

![A planning period with its answers](images/period.png)

## Plan

The whole period on one page, only the weekdays that carry shifts, everybody's remaining
capacity in the sidebar. **Calculate proposal** fills as many shifts as the rules allow and
marks how each person rated the shift they got: preferred, available, if needed, or sitting in.
What nobody can cover stays visibly empty.

![The planning view with a calculated proposal](images/planning.png)

## Check and publish

Publication takes the saved shared draft and nothing else. It creates confirmed ephios
participations and sends one summary per person; shifts below their minimum have to be
confirmed first.

![The publication check](images/publication.png)

## Staff and replace

After publication every service shows who answered they are available that day, whether they
could take the shift and what speaks against it.

![The replacement overview](images/replacement.png)

Members do not need that page for the usual case: the service itself carries their own state
and the way out of it.

![A published service with its shift coordination panel](images/service.png)
