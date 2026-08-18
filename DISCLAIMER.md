# Intended use

**ImageSL is research software. It is not a medical device, and it must not be
used for clinical diagnosis, patient management, or any decision affecting the
care of a person.**

It has not been submitted to, cleared by, or approved by the FDA, the EMA, the
MHRA, a Notified Body, or any other regulatory authority. No claim of
diagnostic accuracy, analytical validity, or clinical validity is made.

## What it does

ImageSL measures the area of DAB-positive staining in immunohistochemistry
images and reports that measurement, together with the settings used to
produce it. It is a measuring instrument, not an interpreter: it does not
diagnose, grade, stage, or classify.

## What that means in practice

* **Validate before you rely on it.** Measurements depend on staining
  protocol, scanner, illumination, and section thickness. Establish agreement
  against your own ground truth on your own material before using any number
  from this software in an analysis.
* **Report your settings.** The sensitivity level and any manual Include or
  Exclude regions change the result. They are recorded in every export for
  exactly this reason; publish them alongside the measurement so the result can
  be reproduced.
* **Manual corrections are subjective.** Include and Exclude are operator
  judgement. Where they are used, say so.
* **Version matters.** Detection behaviour can change between releases. Record
  the version, which appears in every CSV export and in Settings.

## Data handling

The desktop application performs all analysis locally. Images are not
transmitted. The only network request it makes is an update check. See
`server/web/privacy.html` for details.

## No warranty

This software is provided without warranty of any kind. See `LICENSE`.
