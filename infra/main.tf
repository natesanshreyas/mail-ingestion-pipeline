# =============================================================================
# mail-ingestion-pipeline — Terraform infrastructure
# =============================================================================

locals {
  p = var.prefix
}

data "azurerm_client_config" "current" {}

# -----------------------------------------------------------------------------
# Resource Group
# -----------------------------------------------------------------------------

resource "azurerm_resource_group" "rg" {
  name     = "rg-${local.p}"
  location = var.location
  tags     = var.tags
}

# -----------------------------------------------------------------------------
# Azure Container Registry
# -----------------------------------------------------------------------------

resource "azurerm_container_registry" "acr" {
  name                = "acr${local.p}"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  sku                 = "Basic"
  admin_enabled       = false
  tags                = var.tags
}

# -----------------------------------------------------------------------------
# AKS Cluster
# -----------------------------------------------------------------------------

resource "azurerm_kubernetes_cluster" "aks" {
  name                = "aks-${local.p}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  dns_prefix          = local.p
  tags                = var.tags

  default_node_pool {
    name                = "system"
    node_count          = var.aks_node_count
    vm_size             = var.aks_node_vm_size
    enable_auto_scaling = true
    min_count           = 1
    max_count           = 5
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin = "azure"
    network_policy = "azure"
  }
}

# Grant AKS kubelet identity the AcrPull role on the registry
resource "azurerm_role_assignment" "aks_acr_pull" {
  scope                = azurerm_container_registry.acr.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_kubernetes_cluster.aks.kubelet_identity[0].object_id
}

# -----------------------------------------------------------------------------
# Azure Service Bus — Graph pushes change notifications directly to this queue
# -----------------------------------------------------------------------------

resource "azurerm_servicebus_namespace" "asb" {
  name                = "asb-${local.p}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  sku                 = "Standard"
  tags                = var.tags
}

resource "azurerm_servicebus_queue" "notifications" {
  name         = "mail-notifications"
  namespace_id = azurerm_servicebus_namespace.asb.id

  # Keep unprocessed messages for 7 days before dead-lettering
  max_delivery_count              = 10
  default_message_time_to_live    = "P7D"
  dead_lettering_on_message_expiration = true
  lock_duration                   = "PT1M"
}

# Separate auth rules — least privilege
resource "azurerm_servicebus_queue_authorization_rule" "send" {
  name     = "graph-send"
  queue_id = azurerm_servicebus_queue.notifications.id
  send     = true
  listen   = false
  manage   = false
}

resource "azurerm_servicebus_queue_authorization_rule" "listen" {
  name     = "worker-listen"
  queue_id = azurerm_servicebus_queue.notifications.id
  send     = false
  listen   = true
  manage   = false
}

# -----------------------------------------------------------------------------
# Azure Event Hubs — output destination for classified email JSON
# -----------------------------------------------------------------------------

resource "azurerm_eventhub_namespace" "ehn" {
  name                = "ehn-${local.p}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  sku                 = "Standard"
  capacity            = 1
  tags                = var.tags
}

resource "azurerm_eventhub" "hub" {
  name                = "email-intents"
  namespace_name      = azurerm_eventhub_namespace.ehn.name
  resource_group_name = azurerm_resource_group.rg.name
  partition_count     = var.eventhub_partition_count
  message_retention   = var.eventhub_retention_days
}

resource "azurerm_eventhub_consumer_group" "preview" {
  name                = "preview"
  namespace_name      = azurerm_eventhub_namespace.ehn.name
  eventhub_name       = azurerm_eventhub.hub.name
  resource_group_name = azurerm_resource_group.rg.name
}

resource "azurerm_eventhub_authorization_rule" "send" {
  name                = "send-rule"
  namespace_name      = azurerm_eventhub_namespace.ehn.name
  eventhub_name       = azurerm_eventhub.hub.name
  resource_group_name = azurerm_resource_group.rg.name
  listen              = false
  send                = true
  manage              = false
}

resource "azurerm_eventhub_authorization_rule" "listen" {
  name                = "listen-rule"
  namespace_name      = azurerm_eventhub_namespace.ehn.name
  eventhub_name       = azurerm_eventhub.hub.name
  resource_group_name = azurerm_resource_group.rg.name
  listen              = true
  send                = false
  manage              = false
}

# -----------------------------------------------------------------------------
# Cosmos DB — mailbox & subscription tracking
# -----------------------------------------------------------------------------

resource "azurerm_cosmosdb_account" "cosmos" {
  name                = "cosmos-${local.p}"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  offer_type          = "Standard"
  kind                = "GlobalDocumentDB"
  tags                = var.tags

  consistency_policy {
    consistency_level = "Session"
  }

  geo_location {
    location          = azurerm_resource_group.rg.location
    failover_priority = 0
  }
}

resource "azurerm_cosmosdb_sql_database" "db" {
  name                = "mail-ingestion"
  resource_group_name = azurerm_resource_group.rg.name
  account_name        = azurerm_cosmosdb_account.cosmos.name
}

resource "azurerm_cosmosdb_sql_container" "mailboxes" {
  name                = "mailboxes"
  resource_group_name = azurerm_resource_group.rg.name
  account_name        = azurerm_cosmosdb_account.cosmos.name
  database_name       = azurerm_cosmosdb_sql_database.db.name
  partition_key_path  = "/mailboxId"
  throughput          = var.cosmos_throughput
}

# -----------------------------------------------------------------------------
# Storage Account — delta token cache (one blob per mailbox)
# -----------------------------------------------------------------------------

resource "azurerm_storage_account" "st" {
  name                     = "st${local.p}"
  resource_group_name      = azurerm_resource_group.rg.name
  location                 = azurerm_resource_group.rg.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  tags                     = var.tags
}

resource "azurerm_storage_container" "delta_tokens" {
  name                  = "delta-tokens"
  storage_account_name  = azurerm_storage_account.st.name
  container_access_type = "private"
}

# -----------------------------------------------------------------------------
# Key Vault — centralised secret store
# -----------------------------------------------------------------------------

resource "azurerm_key_vault" "kv" {
  name                       = "kv-${local.p}"
  location                   = azurerm_resource_group.rg.location
  resource_group_name        = azurerm_resource_group.rg.name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  soft_delete_retention_days = 7
  purge_protection_enabled   = false
  tags                       = var.tags

  access_policy {
    tenant_id = data.azurerm_client_config.current.tenant_id
    object_id = data.azurerm_client_config.current.object_id
    secret_permissions = ["Get", "List", "Set", "Delete", "Purge"]
  }
}

resource "azurerm_key_vault_secret" "graph_client_state" {
  name         = "graph-client-state"
  value        = var.graph_client_state
  key_vault_id = azurerm_key_vault.kv.id
}

resource "azurerm_key_vault_secret" "openai_api_key" {
  name         = "openai-api-key"
  value        = var.openai_api_key
  key_vault_id = azurerm_key_vault.kv.id
}

resource "azurerm_key_vault_secret" "asb_send_cs" {
  name         = "asb-send-connection-string"
  value        = azurerm_servicebus_queue_authorization_rule.send.primary_connection_string
  key_vault_id = azurerm_key_vault.kv.id
}

resource "azurerm_key_vault_secret" "asb_listen_cs" {
  name         = "asb-listen-connection-string"
  value        = azurerm_servicebus_queue_authorization_rule.listen.primary_connection_string
  key_vault_id = azurerm_key_vault.kv.id
}

resource "azurerm_key_vault_secret" "eventhub_send_cs" {
  name         = "eventhub-send-connection-string"
  value        = azurerm_eventhub_authorization_rule.send.primary_connection_string
  key_vault_id = azurerm_key_vault.kv.id
}

resource "azurerm_key_vault_secret" "storage_cs" {
  name         = "storage-connection-string"
  value        = azurerm_storage_account.st.primary_connection_string
  key_vault_id = azurerm_key_vault.kv.id
}

resource "azurerm_key_vault_secret" "cosmos_key" {
  name         = "cosmos-primary-key"
  value        = azurerm_cosmosdb_account.cosmos.primary_key
  key_vault_id = azurerm_key_vault.kv.id
}
