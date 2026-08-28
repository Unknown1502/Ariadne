variable "project_id" {
  type        = string
  description = "Google Cloud project ID."
}

variable "region" {
  type        = string
  default     = "asia-south1"
  description = "Region for Cloud Run, Cloud SQL, Firestore, and Artifact Registry."
}

variable "image" {
  type        = string
  description = "Container image for the API and worker. Both roles share one image so the worker cannot drift from the code that produced the evidence."
}

variable "console_image" {
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello:latest"
  description = "Container image for the investigation console (nginx serving the built React app, proxying /api and /health to the API service)."
}

variable "llm_provider" {
  type        = string
  default     = "gemini"
  description = "gemini | stub. `stub` is the offline deterministic reasoner and is labelled as such in the API and console."

  validation {
    condition     = contains(["gemini", "stub"], var.llm_provider)
    error_message = "llm_provider must be gemini or stub."
  }
}

variable "model_topic" {
  type        = string
  default     = "ariadne.model-events"
  description = "Topic carrying MODEL_VERSION_DEPLOYED and DISTRIBUTION_CHANGED."
}

variable "dead_letter_topic" {
  type        = string
  default     = "ariadne.dead-letter"
  description = "Events that exhausted their delivery attempts. Parked, never dropped."
}

variable "max_delivery_attempts" {
  type        = number
  default     = 5
  description = "Pub/Sub attempts before dead-lettering. The worker is idempotent, so redelivery is safe."

  validation {
    condition     = var.max_delivery_attempts >= 5 && var.max_delivery_attempts <= 100
    error_message = "Pub/Sub requires max_delivery_attempts between 5 and 100."
  }
}

variable "sql_instance_name" {
  type        = string
  default     = "ariadne-evidence"
  description = "Cloud SQL instance holding the append-only evidence ledger."
}

variable "sql_tier" {
  type        = string
  default     = "db-f1-micro"
  description = "Smallest tier. The ledger is small; experiment execution is local arithmetic."
}

variable "max_instances" {
  type        = number
  default     = 2
  description = "Cloud Run instance cap. Combined with min_instance_count 0, this bounds cost."
}

variable "billing_account" {
  type        = string
  default     = ""
  description = "Billing account ID. Leave empty to skip the budget guardrail."
}

variable "budget_usd" {
  type        = number
  default     = 50
  description = "Budget alert threshold in USD."
}
