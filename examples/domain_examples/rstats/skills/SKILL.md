---
name: rstats-basics
description: How to do data analysis in R inside execute_rstats_code — load data, wrangle with data.table, compute summaries, fit models, and return plots/files as artifacts.
---

# Working in the R environment

You are writing **R**, executed in a persistent R kernel. State (variables,
loaded packages) survives across calls within a session, so build up an
analysis incrementally. `data.table` and `jsonlite` are already loaded.

## Returning results

- **Values**: just evaluate them — the last expression's value is captured,
  e.g. `summary(model)` or `head(dt)`.
- **Structured output** for downstream steps: emit JSON with
  `jsonlite::toJSON(x, auto_unbox = TRUE, pretty = TRUE)`.
- **Files and plots**: write them into `AGORA_OUTPUT_DIR` (a path already set
  in the environment) and they are returned as downloadable artifacts:

  ```r
  png(file.path(AGORA_OUTPUT_DIR, "hist.png"), width = 800, height = 600)
  hist(rnorm(1000), main = "Sample")
  dev.off()
  ```

## Loading data

Prefer `data.table::fread()` — it is fast and handles CSV/TSV/URLs:

```r
dt <- fread("https://example.com/data.csv")
str(dt)
```

For built-in datasets you can use `data("mtcars")`, `iris`, etc.

## Wrangling with data.table

```r
dt <- as.data.table(mtcars)
# filter rows, select/compute columns, group-aggregate in one call:
dt[mpg > 20, .(mean_hp = mean(hp), n = .N), by = cyl]
```

## Summaries and models

```r
summary(dt)                          # per-column summary stats
fit <- lm(mpg ~ wt + hp, data = dt)  # linear model
summary(fit)                         # coefficients, R-squared, etc.
```

## Plotting

Base graphics always work. For nicer plots, `library(ggplot2)` first:

```r
library(ggplot2)
p <- ggplot(dt, aes(wt, mpg, color = factor(cyl))) + geom_point()
ggsave(file.path(AGORA_OUTPUT_DIR, "scatter.png"), p, width = 7, height = 5)
```

## Tips

- This kernel is R: use `<-` for assignment, `library()` to load packages,
  and `?fun` semantics do not apply (no interactive help) — write code that
  runs to completion.
- Need a package that is not installed? Note it back to the user rather than
  attempting network installs from inside a snippet.
