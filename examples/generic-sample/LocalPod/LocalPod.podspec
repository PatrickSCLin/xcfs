Pod::Spec.new do |s|
  s.name = 'LocalPod'; s.version = '0.1.0'; s.summary = 'A local pod for the xcfs sample'; s.homepage = 'https://example.com'; s.license = 'MIT'
  s.author = 'example'; s.source = { :path => '.' }; s.ios.deployment_target = '17.0'; s.swift_version = '5.9'
  s.source_files = 'Sources/**/*.swift'
end
