class Client {
  getRepo(owner, name) {
    return this.request('GET', `repos/${owner}/${name}`)
  }
  createIssue(owner, name, body) {
    return this.request("POST", `repos/${owner}/${encodeURIComponent(name)}/issues`, body)
  }
  dynamic(kind) {
    return this.request('GET', BASE + kind)
  }
  me() {
    return this.request('GET', 'user', { headers: { Authorization: 'Bearer ghp_abcdefghijklmnopqrstuvwxyz0123456789', 'X-Client': 'demo' } })
  }
  status() {
    return fetch('https://admin:hunter2@api.demo.test/v1/status', { method: 'GET' })
  }
  remove(id) {
    return axios.delete(`/v1/items/${id}`)
  }
  unknown(options) {
    return fetch('/v1/unknown-method', options)
  }
}
