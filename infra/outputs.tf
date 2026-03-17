output "resource_group_name" {
  value = azurerm_resource_group.rg.name
}

output "acr_login_server" {
  value = azurerm_container_registry.acr.login_server
}

output "aks_cluster_name" {
  value = azurerm_kubernetes_cluster.aks.name
}

output "aks_get_credentials_command" {
  value = "az aks get-credentials --resource-group ${azurerm_resource_group.rg.name} --name ${azurerm_kubernetes_cluster.aks.name} --overwrite-existing"
}

output "asb_namespace" {
  value = azurerm_servicebus_namespace.asb.name
}

output "asb_queue_name" {
  value = azurerm_servicebus_queue.notifications.name
}

output "asb_send_connection_string" {
  description = "Used by the reconciler to build the ServiceBus: notification URL passed to Graph subscriptions."
  value       = azurerm_servicebus_queue_authorization_rule.send.primary_connection_string
  sensitive   = true
}

output "asb_listen_connection_string" {
  description = "Used by the ingestion worker to consume messages from the queue."
  value       = azurerm_servicebus_queue_authorization_rule.listen.primary_connection_string
  sensitive   = true
}

output "eventhub_namespace" {
  value = azurerm_eventhub_namespace.ehn.name
}

output "eventhub_send_connection_string" {
  description = "Used by the ingestion worker to publish classified JSON."
  value       = azurerm_eventhub_authorization_rule.send.primary_connection_string
  sensitive   = true
}

output "eventhub_listen_connection_string" {
  description = "For downstream consumers reading classified email JSON."
  value       = azurerm_eventhub_authorization_rule.listen.primary_connection_string
  sensitive   = true
}

output "cosmos_endpoint" {
  value = azurerm_cosmosdb_account.cosmos.endpoint
}

output "cosmos_primary_key" {
  value     = azurerm_cosmosdb_account.cosmos.primary_key
  sensitive = true
}

output "storage_connection_string" {
  value     = azurerm_storage_account.st.primary_connection_string
  sensitive = true
}

output "key_vault_uri" {
  value = azurerm_key_vault.kv.vault_uri
}
