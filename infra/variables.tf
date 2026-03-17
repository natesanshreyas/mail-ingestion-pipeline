variable "prefix" {
  description = "Short prefix applied to all resource names (3-8 lowercase alphanumeric chars)."
  type        = string
  default     = "mailingest"
}

variable "location" {
  description = "Azure region for all resources."
  type        = string
  default     = "eastus"
}

variable "graph_client_state" {
  description = "Secret string embedded in every Graph change-notification subscription. Must match GRAPH_CLIENT_STATE in the webhook service."
  type        = string
  sensitive   = true
}

variable "openai_api_key" {
  description = "OpenAI API key used by the ingestion worker for email classification."
  type        = string
  sensitive   = true
}

variable "aks_node_count" {
  description = "Initial number of AKS worker nodes."
  type        = number
  default     = 2
}

variable "aks_node_vm_size" {
  description = "VM size for AKS nodes."
  type        = string
  default     = "Standard_B2s"
}

variable "eventhub_partition_count" {
  description = "Number of partitions for the email-intents Event Hub."
  type        = number
  default     = 4
}

variable "eventhub_retention_days" {
  description = "Message retention (days) for the email-intents Event Hub."
  type        = number
  default     = 7
}

variable "cosmos_throughput" {
  description = "Provisioned RU/s for the Cosmos DB mailboxes container."
  type        = number
  default     = 400
}

variable "tags" {
  description = "Tags applied to all resources."
  type        = map(string)
  default = {
    project     = "mail-ingestion-pipeline"
    environment = "production"
  }
}
