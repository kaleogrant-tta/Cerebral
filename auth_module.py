import streamlit as st
import hashlib
import time
import streamlit.components.v1 as components

REMEMBER_SALT = "cerebral_remember_salt_v1"

def find_secret(key: str, secrets_dict=None):
    """Recursively search st.secrets for a key, regardless of TOML nesting."""
    if secrets_dict is None:
        secrets_dict = st.secrets
    if key in secrets_dict and not isinstance(secrets_dict[key], dict):
        return secrets_dict[key]
    for v in secrets_dict.values():
        if isinstance(v, dict):
            result = find_secret(key, v)
            if result is not None:
                return result
    return None

def get_remember_token():
    correct_hash = find_secret("app_password_hash")
    if not correct_hash:
        return ""
    return hashlib.sha256((correct_hash + REMEMBER_SALT).encode()).hexdigest()

# ========== STEP 1: HANDLE LOGOUT FIRST ==========
if st.query_params.get("logout") == "1":
    components.html("""
    <script>
    localStorage.removeItem('cerebral_auth_token');
    const url = new URL(window.location.href);
    url.searchParams.delete('logout');
    window.location.replace(url.toString());
    </script>
    """, height=0)
    st.stop()

# ========== STEP 2: SESSION STATE INIT ==========
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
if "remembered" not in st.session_state:
    st.session_state.remembered = False

# ========== STEP 3: AUTO-LOGIN FROM BROWSER STORAGE ==========
query_token = st.query_params.get("auth_token")
if query_token and query_token == get_remember_token():
    st.session_state.authenticated = True
    st.session_state.last_activity = time.time()
    st.session_state.remembered = True
    st.query_params.clear()

# ========== STEP 4: LOGIN SCREEN ==========
if not st.session_state.authenticated:
    st.title("🔒 Cerebral")
    
    correct_hash = find_secret("app_password_hash")
    if not correct_hash:
        st.error("⚠️ Add app_password_hash to Streamlit Secrets")
        st.stop()
    
    # Check if browser has saved token and redirect
    components.html("""
    <script>
    (function() {
        const token = localStorage.getItem('cerebral_auth_token');
        if (token) {
            const url = new URL(window.location.href);
            if (!url.searchParams.has('auth_token')) {
                url.searchParams.set('auth_token', token);
                window.location.replace(url.toString());
            }
        }
    })();
    </script>
    """, height=0)
    
    password = st.text_input("Password", type="password")
    remember = st.checkbox("Remember this device for 30 days", value=False)
    
    if st.button("Login"):
        if hashlib.sha256(password.encode()).hexdigest() == correct_hash:
            st.session_state.authenticated = True
            st.session_state.last_activity = time.time()
            
            if remember:
                token = get_remember_token()
                components.html(f"""
                <script>
                localStorage.setItem('cerebral_auth_token', '{token}');
                </script>
                """, height=0)
                st.session_state.remembered = True
                st.success("This device is now remembered.")
                if st.button("Continue to Dashboard"):
                    st.rerun()
            else:
                st.rerun()
        else:
            st.error("Incorrect password")
    
    st.stop()

# ========== STEP 5: SESSION TIMEOUT ==========
if "last_activity" not in st.session_state:
    st.session_state.last_activity = time.time()

timeout = 2592000 if st.session_state.get("remembered") else 3600
if time.time() - st.session_state.last_activity > timeout:
    st.session_state.authenticated = False
    st.session_state.remembered = False
    st.rerun()

st.session_state.last_activity = time.time()

# ========== STEP 6: LOGOUT BUTTON ==========
if st.sidebar.button("🚪 Logout"):
    st.session_state.authenticated = False
    st.session_state.remembered = False
    st.query_params["logout"] = "1"
    st.rerun()
