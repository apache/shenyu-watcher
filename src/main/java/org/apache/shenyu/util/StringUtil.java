/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package org.apache.shenyu.util;

import java.util.HashMap;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public class StringUtil {

    public static Map<String, String> parse(final String fileName) {
        Map<String, String> map = new HashMap<>(8);
        String pattern = "([-]\\d.*)\\.jar";
        Pattern r = Pattern.compile(pattern);
        Matcher matcher = r.matcher(fileName);
        if (matcher.find()) {
            String versionGroup = matcher.group(0);
            String packageName = fileName.replace(versionGroup, "");
            String version = versionGroup.replace(".jar", "").substring(1);
            map.put("packageName", packageName);
            map.put("version", version);
            map.put("original", fileName);
        }
        map.put("original", fileName);
        return map;
    }

    public static boolean match(String content, String checkTarget) {
        Pattern pattern = Pattern.compile(checkTarget);

        Matcher matcher = pattern.matcher(content);

        return matcher.find();
    }

}
